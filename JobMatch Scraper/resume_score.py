"""
resume_score.py — grade a résumé 0–100 offline, the way a recruiter screen and an ATS do.

Why this exists: JobMatch already knows how well a résumé matches ONE job (core.score_against →
the feed's match %). It had no answer for "is this résumé any good at all", which is the question
every rewrite starts from. This module answers that half — a fixed rubric of checks, each scored
0–10, combined by weight into one number out of 100.

Three deliberate constraints:

  * No network, no API key, no model. Every check here is regex, lexicon or layout, so a score is
    instant and free. The judgements that genuinely need a model ("is this bullet an accomplishment
    or a restated duty?") are NOT faked with a keyword list — they are absent, and resume_brain.ai
    handles them on the rewrite path where a model is already in play.
  * Deterministic. Same text in, same score out, so "did my edit help?" is answerable by
    re-scoring. A grader whose number drifts is not a measurement.
  * Every penalty names the lines that caused it. A score with no offenders attached is not
    actionable, and unactionable feedback is what makes most résumé graders useless.

Section detection is local rather than reused from resume_brain.latex on purpose: that module's
_is_heading only accepts ALL-CAPS or trailing-colon headings, so a résumé with ordinary Title Case
headings (Professional Experience) parses as one giant untitled section. Most real résumés are
Title Case, and this rubric is meaningless without knowing which section a line is in.

The band thresholds are provisional: they come from the rubric, not from outcomes. Once enough
applications carry a result, the weights can be fitted to real callbacks instead — the one thing a
standalone résumé grader can never do and JobMatch can.
"""
import json
import os
import re
from collections import Counter

from resume_brain.export import _BULLET      # one definition of "this line is a bullet"
from resume_brain import voice               # one definition of how our writing should sound

# A dense résumé page is ~3.5k characters of extracted text. Only used for a page ESTIMATE — the
# real page count depends on the file layout, which plain text no longer carries.
PAGE_CHARS = 3500
MAX_OFFENDERS = 5                            # per check, to keep a report readable

LEVELS = ("entry", "mid", "senior")

# ----------------------------------------------------------------------------- section detection
_SECTION_KW = (
    "summary", "objective", "profile", "education", "experience", "employment", "work history",
    "professional", "skills", "projects", "project", "certification", "certifications",
    "leadership", "awards", "publications", "activities", "technical", "interests", "volunteer",
    "achievements", "training", "languages", "courses", "coursework", "references", "ventures",
)
_SECTION_RE = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(k) for k in _SECTION_KW), re.I)

_EXPERIENCE_KW = ("experience", "employment", "work history", "professional", "projects",
                  "project", "leadership", "activities", "volunteer", "achievements", "ventures")
_SKILLS_KW = ("skills", "technical", "languages", "competencies")
_EDU_KW = ("education", "coursework", "courses", "training", "certification", "certifications")
_SUMMARY_KW = ("summary", "objective", "profile")


def _looks_like_heading(line):
    """A section heading, in any casing. Deliberately permissive about case and strict about
    shape: short, few words, no sentence-ending punctuation, and it names a known section."""
    if _BULLET.match(line):
        return False
    s = line.strip().rstrip(":").strip()
    if not s or len(s) > 60 or len(s.split()) > 6:
        return False
    if s.endswith((".", ",", ";", "!", "?")):
        return False
    return bool(_SECTION_RE.search(s))


def _group_of(title):
    low = (title or "").lower()
    for kws, name in ((_EXPERIENCE_KW, "experience"), (_SKILLS_KW, "skills"),
                      (_EDU_KW, "education"), (_SUMMARY_KW, "summary")):
        if any(k in low for k in kws):
            return name
    return "other"


def _lines_with_offsets(text):
    """[(line_without_terminator, start_offset)] over `text`.

    keepends=True and a running cursor, because the terminator length is exactly what plain
    splitlines() throws away — and getting it wrong by one per line is how every span below the
    first CRLF ends up pointing at the wrong characters.
    """
    out, pos = [], 0
    for raw in (text or "").splitlines(keepends=True):
        body = raw.rstrip("\r\n")
        out.append((body, pos))
        pos += len(raw)
    return out


def _locate(raw, line_start, needle):
    """Absolute (start, end) of `needle` inside the line that begins at `line_start`.

    `needle` is always a whitespace/bullet-stripped slice of `raw`, so index() finds it; the guard
    is for the pathological case where a caller passes something else, and it degrades to the whole
    line rather than raising or silently returning offset 0 (which would highlight the résumé's
    first characters — a wrong highlight is worse than none).
    """
    if not needle:
        return line_start, line_start + len(raw)
    try:
        off = raw.index(needle)
    except ValueError:
        return line_start, line_start + len(raw)
    return line_start + off, line_start + off + len(needle)


_ROLEISH_RE = re.compile(r"\b(?:19|20)\d{2}\b|\bpresent\b|\bcurrent\b", re.I)


def _is_continuation(raw, prev, prev_indent):
    """Is this line the rest of the previous bullet, wrapped by the PDF?

    THE BUG THIS FIXES. A PDF lays a long bullet across several visual lines and extraction returns
    one line per visual line, with nothing marking which are continuations. Only the first carries
    the bullet glyph, so `_experience_items` (which keeps glyph lines) DROPPED the rest — including
    the half with the number in it. Every PDF-sourced résumé was scored on fragments, and bullets
    that plainly stated a result were told they had none.

    Two signals, both high precision:
      * deeper indentation than the bullet's own glyph — how wrapped text is laid out, and
      * a lowercase or punctuation first character — how a sentence continues mid-clause.
    A capitalised line only joins when the previous one was left hanging AND this one carries no
    date and is not Title Case throughout, because those are the shapes of a role or employer line,
    which must stay separate.
    """
    if prev is None or not prev.get("bullet"):
        return False
    s = raw.strip()
    if not s or _BULLET.match(raw) or _looks_like_heading(raw):
        return False
    indent = len(raw) - len(raw.lstrip())
    if s[0].islower() or s[0] in ",;:)/&+-":
        return True
    if indent > prev_indent + 1:
        return True
    tail = (prev.get("text") or "").rstrip()
    if tail and tail[-1] not in ".!?" and not _ROLEISH_RE.search(s) and len(s.split()) > 2:
        words = re.findall(r"[A-Za-z']+", s)
        if not (words and all(w[0].isupper() for w in words)):
            return True
    return False


def split_sections(text):
    """Plain text -> (header_lines, sections).

    sections = [{title, group, start, end, items:[{text, bullet, start, end}]}], where start/end
    are character offsets into the ORIGINAL text. The offsets are the whole reason this returns
    more than strings: the review panel highlights the exact spans a check objected to, and an
    offender string cannot be searched back into the document reliably (it has been stripped,
    possibly truncated, and may appear twice).

    Anything before the first heading is the header block (name / contact), never an item.
    """
    header, sections, cur = [], [], None
    prev_indent = 0
    for raw, line_start in _lines_with_offsets(text):
        s = raw.strip()
        if not s:
            continue
        if _looks_like_heading(raw):
            title = s.rstrip(":").strip()
            t0, t1 = _locate(raw, line_start, title)
            cur = {"title": title, "group": _group_of(title), "items": [],
                   "start": t0, "end": t1}
            sections.append(cur)
            continue
        if cur is None:
            header.append(s)
            continue
        m = _BULLET.match(raw)
        body = (m.group(1) if m else s).strip()
        b0, b1 = _locate(raw, line_start, body)
        prev = cur["items"][-1] if cur["items"] else None
        if _is_continuation(raw, prev, prev_indent):
            # Fold the wrapped remainder into the bullet it belongs to. `frags` keeps each visual
            # line's own range so a highlight never has to span a newline, while `end` grows to
            # cover the whole logical bullet.
            prev["text"] = (prev["text"] + " " + body).strip()
            prev["end"] = b1
            prev["frags"].append((b0, b1))
            continue
        cur["items"].append({"text": body, "bullet": bool(m), "start": b0, "end": b1,
                             "frags": [(b0, b1)]})
        prev_indent = len(raw) - len(raw.lstrip())
    return header, sections


def header_span(text, sections):
    """The header block's extent: start of document to the first heading. Derived rather than
    stored so split_sections keeps returning plain strings for the header and no call site
    changes. Used by the contact check, whose complaint is about a REGION, not a phrase."""
    end = sections[0]["start"] if sections else len(text or "")
    return 0, max(0, end)


def _experience_items(sections):
    """Bullets that carry accomplishments, and whether a real experience section was found.

    Only the BULLETS: an experience section also holds role, employer and date lines, and those
    never open with an action verb or carry a metric. Counting them as bullets penalised every
    résumé for its own job titles and diluted the quantified ratio with lines that cannot be
    quantified. Sections with no bullet glyph at all fall back to their plain lines, since there
    the lines really are the content.

    Falls back to every bullet in the document when no experience section was recognised, so a
    badly-headed résumé is still scored on impact instead of scoring 0 on every impact check —
    a parse failure must not masquerade as a writing failure.
    """
    exp = [s for s in sections if s["group"] == "experience"]
    if exp:
        items = [i for s in exp for i in s["items"] if i["bullet"]]
        return (items or [i for s in exp for i in s["items"]]), True
    return [i for s in sections for i in s["items"] if i["bullet"]], False


def _experience_text(sections):
    """Just the experience sections, for checks that must not be judged on the education block."""
    return "\n".join(i["text"] for s in sections if s["group"] == "experience"
                     for i in s["items"])


# ------------------------------------------------------------------------------------- lexicons
# Weak but real verbs. "responsible" stays here despite being an adjective because "Responsible
# for" is the canonical weak opener and the fix text names it; the purely adjectival ones
# ("various", "familiar") moved to _NONVERB_OPENERS so the two checks stay disjoint and one bad
# line is never billed twice.
_WEAK_OPENERS = frozenset("""
assisted assist helped help aided aid supported support participated participate involved
contributed contribute worked work responsible tasked handled handle dealt attended attend
shadowed observed engaged exposed learned utilized utilised used using
""".split())

# Irregular past tenses a naive -ed test would miss, plus the present forms a current role
# legitimately uses. Regular -ed verbs are accepted without being listed.
_STRONG_PAST = frozenset("""
led built drove ran won grew cut saved wrote spoke taught brought chose made took gave held kept
sent set met sold spent told found drew began broke rebuilt oversaw undertook struck
""".split())
_PRESENT_FORMS = frozenset("""
lead build drive run manage own design develop deliver launch grow cut save write teach bring
choose make take give hold keep send set meet sell spend tell find draw begin break oversee
architect analyze analyse automate maintain mentor negotiate optimize optimise partner present
report scale ship support train
""".split())

# Openers that cannot be a verb: determiners, prepositions, and the noun-phrase starters that mark
# a bullet describing a role rather than an action. Words already in _WEAK_OPENERS are left out so
# one bad line is not billed twice to two different checks.
_NONVERB_OPENERS = frozenset("""
a an the this that these those my our their its his her
in on at for with as during through via by from to of about under over across within
key member part duties responsibilities accomplishments tasks role roles position skills team
project projects successful strong excellent proven extensive solid significant core primary
various familiar numerous multiple
""".split())

# Moved verbatim to resume_brain/voice.py so the two AI prompts and this scorer share one list.
# NOT the same set as voice.AI_SLOP: that one holds the words a model reaches for unprompted
# ("spearheaded", "leveraged", "robust"), which constrain the prompts and surface as an ADVISORY
# check only. Scoring them would both re-calibrate every band and make "spearheaded" earn credit
# under leadership_signals while losing points here. See the note in voice.py.
_FILLER = voice.FILLER
_ADVERBS = frozenset("""
successfully effectively efficiently skillfully skilfully significantly substantially greatly
highly extremely very really quickly closely actively heavily strongly consistently diligently
proactively seamlessly robustly meaningfully
""".split())
_PRONOUNS = frozenset(("i", "me", "my", "mine", "we", "us", "our", "ours"))

_LEAD_VERBS = frozenset("""
led managed mentored directed coordinated spearheaded owned drove chaired supervised trained
coached founded launched headed oversaw established initiated
""".split())
_SCOPE_RE = re.compile(
    r"\b(?:team of|group of|staff of|cross[- ]functional|stakeholders?|direct reports?|"
    r"\d+\s*(?:people|engineers|analysts|interns|developers|members|reports|contractors))\b", re.I)

_PERSONAL_INFO_RE = re.compile(
    r"\b(?:marital status|date of birth|d\.o\.b|nationality|gender|religion|"
    r"age\s*[:\-]\s*\d{1,2})\b", re.I)
_REFERENCES_RE = re.compile(r"references\s+available|references\s+upon\s+request", re.I)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d{1,2}[\s.\-]?)?\(?\d{3}\)?[\s.\-]\s?\d{3}[\s.\-]\d{4}")
_LOCATION_RE = re.compile(r"\b[A-Z][a-zA-Z .\-]{2,24},\s*(?:[A-Z]{2}\b|[A-Z][a-z]+\b)")
_LINKEDIN_RE = re.compile(r"linkedin\.com/[A-Za-z0-9_\-%/]+", re.I)

# Date shapes, each a distinct FORMAT. Mixing them is the inconsistency recruiters notice.
_DATE_FORMATS = (
    ("Mon YYYY", re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\b")),
    ("MM/YYYY", re.compile(r"\b\d{1,2}/\d{2,4}\b")),
    ("YYYY-MM", re.compile(r"\b\d{4}-\d{2}\b")),
    ("YYYY", re.compile(r"(?<![\d/\-])(?:19|20)\d{2}(?![\d/\-])")),
)
_YEARISH_RE = re.compile(r"\b(?:19|20)\d{2}\b|\b\d{1,2}/\d{1,4}\b|\b\d{4}-\d{2}\b")
_METRIC_RE = re.compile(
    r"\d|\b(?:half|third|quarter|doubled|tripled|dozens|hundreds|thousands|millions)\b", re.I)


def _date_formats_in(text):
    """Which date FORMATS appear, most specific first, each consumed before the looser ones run.

    Without the consuming step the bare-YYYY pattern also matches the year inside "Jan 2024", so
    every résumé using month-year dates would be reported as mixing two formats — the check would
    never once return "consistent".
    """
    work, out = text or "", []
    for name, rx in _DATE_FORMATS:
        if rx.search(work):
            out.append(name)
            work = rx.sub(" ", work)
    return out


def _first_word(s):
    m = re.match(r"[^A-Za-z]*([A-Za-z][A-Za-z'\-]*)", s or "")
    return (m.group(1) if m else "").lower()


def _is_quantified(text):
    """A metric, not a date. Strip year/date shapes first — otherwise every bullet under a role
    headed '2021 – 2023' counts as quantified and the heaviest check in the rubric reads 10/10."""
    return bool(_METRIC_RE.search(_YEARISH_RE.sub(" ", text or "")))


def _opens_with_nonverb(text):
    """True only when the opener is CLEARLY not a verb.

    Deliberately inverted. Without a POS tagger, an English present-tense verb is
    indistinguishable from a noun ("Review 400+ transactions" opens with a perfectly good verb
    that no practical whitelist contains), so a whitelist approach flags correct bullets. Every
    false offender shown to the user costs trust in the entire report, so this check trades
    recall for precision: it fires on determiners, prepositions and noun-phrase openers, which
    are the shapes that actually indicate a bullet not built around an action.
    """
    w = _first_word(text)
    if not w:
        return True
    if w in _STRONG_PAST or w in _PRESENT_FORMS or (len(w) > 4 and w.endswith("ed")):
        return False
    return w in _NONVERB_OPENERS


def _score_ratio(ratio, good, bad):
    """10 at/beyond `good`, 0 at/beyond `bad`, linear between. Direction-agnostic, so a check
    where lower is better simply passes good < bad."""
    if good == bad:
        return 10.0 if ratio == good else 0.0
    t = (ratio - bad) / float(good - bad)
    return round(max(0.0, min(1.0, t)) * 10, 1)


def _phrase_hits(text, phrases):
    low = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    return [p for p in phrases if re.search(r"\b%s\b" % re.escape(p), low)]


def _word_hits(text, words):
    return sorted(set(w for w in re.findall(r"[A-Za-z'\-]+", (text or "").lower()) if w in words))


def _clip(texts):
    return [t[:160] for t in texts[:MAX_OFFENDERS]]


def _spans(items):
    """Every visual line of each item, as (start, end).

    Per FRAGMENT, not per item: a bullet the PDF wrapped over three lines is one logical item with
    three ranges, and a single start..end span across it would cover the newlines and indentation
    between them — which the viewer would draw as one block highlight swallowing the gaps.

    Deliberately NOT capped at MAX_OFFENDERS: that cap exists so the written fix list stays readable,
    but the viewer has to mark every occurrence or the counts in the rail disagree with the document.
    """
    out = []
    for i in items:
        out.extend(i.get("frags") or ([(i["start"], i["end"])] if "start" in i else []))
    return out


def _first_word_span(item):
    """Just the opening word of a bullet. The weak-verb and tense checks object to that word, not
    to the whole line, and marking three lines of correct prose to point at 'Assisted' reads as if
    the whole bullet were wrong."""
    w = _first_word(item["text"])
    if not w:
        return (item["start"], item["end"])
    low = item["text"].lower()
    off = low.find(w)
    if off < 0:
        return (item["start"], item["end"])
    return (item["start"] + off, item["start"] + off + len(w))


def _re_spans(rx, text, limit=None):
    """Every match of `rx` in the ORIGINAL text, as spans plus the matched strings.

    This is the replacement for _phrase_hits/_word_hits wherever highlighting is wanted: those two
    lowercase and whitespace-normalise before searching, so their offsets do not map back to the
    document at all. Searching the original with a whitespace-tolerant pattern keeps both the count
    and the position honest.
    """
    spans, hits = [], []
    for m in rx.finditer(text or ""):
        spans.append((m.start(), m.end()))
        hits.append(m.group(0))
        if limit and len(spans) >= limit:
            break
    return spans, hits


def _boundary_alt(phrases):
    """A longest-first alternation with non-word lookarounds, matching how jdrender.Highlighter and
    core._term_present already decide a term is "present".

    Two properties are load-bearing. Longest-first stops a short phrase eating a longer one that
    contains it. The lookarounds replace \\b, which fails on tokens ending in punctuation (c++,
    ci-cd) and — the bug that matters here — would let the pronoun "i" match inside "Objective".
    Spaces become \\s+ so a phrase still matches when the résumé wrapped it across two lines.
    """
    parts = [re.escape(p).replace(r"\ ", r"\s+") for p in sorted(phrases, key=len, reverse=True)]
    return re.compile(r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])" % "|".join(parts), re.I)


def _found(rx, hay):
    m = rx.search(hay or "")
    return m.group(0) if m else ""


# --------------------------------------------------------------------------------------- weights
# Importance, not arithmetic convenience: quantified impact is the single thing that moves a
# recruiter screen most, so it carries the most points. Level-dependent entries are dicts.
# Weights follow INFORMATION, not effort. Measured across five real résumés plus two fixtures, ten
# of these checks returned 10/10 for every document — sections present, dates consistent, no
# pronouns, parses cleanly. A check nobody fails cannot tell two résumés apart, so weighting it
# heavily just hands every résumé the same free points and compresses the whole scale at the top
# (every real résumé was landing 86-92, which made "Exceptional" mean "typical"). Those checks are
# still worth RUNNING — failing one is a genuine defect — they are simply worth little when passed.
#
# The weight went to the checks that actually separate documents: quantified impact, the keywords a
# track expects, and whether listed skills are evidenced.
_WEIGHTS = {
    "quantified_impact":   {"entry": 3.0, "mid": 3.5, "senior": 3.5},
    "weak_verb_openers":   2.0,
    "action_verb_openers": 1.25,
    "prose_not_bullets":   0.4,
    "verb_variety":        0.75,
    "tense_consistency":   0.3,
    "bullet_length":       1.25,
    "filler_buzzwords":    1.25,
    "adverbs":             0.4,
    "personal_pronouns":   0.4,
    "passive_voice":       1.0,
    "resume_length":       0.4,
    "sections_present":    0.5,
    "contact_details":     1.25,
    "date_consistency":    0.4,
    "unnecessary_content": 0.3,
    "leadership_signals":  {"entry": 1.25, "mid": 2.0, "senior": 3.0},
    "parse_health":        0.5,
    # The proofread half. Individually small on purpose: a stray double space is real but it is not
    # worth as much as an unquantified bullet, and letting mechanics accumulate weight would let a
    # tidy résumé with nothing to say outscore a substantive one with a typo.
    "punctuation_consistency":    0.6,
    "capitalization_consistency": 0.6,
    "spacing_hygiene":            0.5,
    "repeated_phrases":           0.9,
    "spelling":                   1.25,
    # Skills is the fifth category and carries real weight: "the keywords your target roles expect"
    # is the one thing an ATS screen is literally built to do.
    "skills_demonstrated":        1.5,
    "role_keywords":              2.5,
}

# THE IMPACT GATE.
#
# A weighted average has a structural flaw a recruiter does not: fifteen hygiene checks can outvote
# one Impact failure. A résumé with immaculate spacing, consistent dates and no metrics anywhere
# scored in the eighties, because everything cheap was perfect. No recruiter reads it that way — a
# résumé that never says what it achieved is not a strong résumé however clean it is.
#
# So quantified impact CAPS the total instead of merely contributing to it.
#
# Keyed to the quantified_impact CHECK, not to the Impact group. The group was the obvious choice and
# it does not work: it also contains verb variety, tense and prose-vs-bullets, which almost every
# résumé passes, so a document with zero numbers anywhere still showed Impact ~70 and the cap never
# engaged (a metric-free résumé scored 72). The check is the honest signal.
#
# 0/10 quantified caps the total at 45; 5/10 caps it at 72; 10/10 gives a ceiling of 100 and the gate
# does nothing. It only ever lowers a score, so it cannot manufacture one.
IMPACT_GATE_FLOOR = 45.0
IMPACT_GATE_SLOPE = 5.5
_GROUPS = {
    "quantified_impact": "Impact", "weak_verb_openers": "Impact",
    "action_verb_openers": "Impact", "prose_not_bullets": "Impact",
    "verb_variety": "Impact", "tense_consistency": "Impact",
    "bullet_length": "Brevity & Style", "filler_buzzwords": "Brevity & Style",
    "adverbs": "Brevity & Style", "personal_pronouns": "Brevity & Style",
    "passive_voice": "Brevity & Style", "resume_length": "Brevity & Style",
    "sections_present": "ATS & Format", "contact_details": "ATS & Format",
    "date_consistency": "ATS & Format", "unnecessary_content": "ATS & Format",
    "parse_health": "ATS & Format",
    "leadership_signals": "Growth & Leadership",
    "punctuation_consistency": "Brevity & Style",
    "capitalization_consistency": "ATS & Format",
    "spacing_hygiene": "Brevity & Style",
    "repeated_phrases": "Brevity & Style",
    "spelling": "Brevity & Style",
    "skills_demonstrated": "Skills",
    "role_keywords": "Skills",
}
# Fixed display order, and the same five categories Resume Worded scores. Skills was the one we
# had no equivalent for at all.
GROUP_ORDER = ("Impact", "Brevity & Style", "Skills", "Growth & Leadership", "ATS & Format")


def _weight(key, level):
    w = _WEIGHTS[key]
    return w[level] if isinstance(w, dict) else w


# ---------------------------------------------------------------------------------------- checks
def _check_quantified(items, level):
    if not items:
        return 0.0, "No experience bullets found to check.", []
    quant = [i for i in items if _is_quantified(i["text"])]
    ratio = len(quant) / float(len(items))
    good, bad = (0.8, 0.05) if level == "entry" else (0.95, 0.1)
    bare = [i for i in items if not _is_quantified(i["text"])]
    return (_score_ratio(ratio, good, bad),
            "%d of %d bullets carry a number (%.0f%%). Target %.0f%%."
            % (len(quant), len(items), ratio * 100, good * 100),
            _clip([i["text"] for i in bare]), _spans(bare))


def _check_weak_openers(items, _level):
    if not items:
        return 0.0, "No experience bullets found to check.", []
    weak = [i for i in items if _first_word(i["text"]) in _WEAK_OPENERS]
    ratio = len(weak) / float(len(items))
    return (_score_ratio(ratio, 0.0, 0.12),
            "%d of %d bullets open with a weak verb." % (len(weak), len(items)),
            _clip([i["text"] for i in weak]), [_first_word_span(i) for i in weak])


def _check_action_openers(items, _level):
    if not items:
        return 0.0, "No experience bullets found to check.", []
    noverb = [i for i in items if _opens_with_nonverb(i["text"])]
    ratio = len(noverb) / float(len(items))
    return (_score_ratio(ratio, 0.0, 0.15),
            "%d of %d bullets open with a noun phrase or preposition, not an action."
            % (len(noverb), len(items)),
            _clip([i["text"] for i in noverb]), [_first_word_span(i) for i in noverb])


def _check_prose(sections, _level):
    """Prose paragraphs where bullets belong. A long unbulleted line in an experience section is
    a wall of text a recruiter skims past."""
    exp = [i for s in sections if s["group"] == "experience" for i in s["items"]]
    if not exp:
        return 0.0, "No experience section recognised.", []
    prose = [i for i in exp if not i["bullet"] and len(i["text"]) > 180]
    return (_score_ratio(len(prose), 0, 3),
            "%d prose block(s) in your experience section." % len(prose) if prose
            else "No prose blocks where bullets belong.",
            _clip([i["text"] for i in prose]), _spans(prose))


def _check_verb_variety(items, _level):
    verbs = [_first_word(i["text"]) for i in items if not _opens_with_nonverb(i["text"])]
    if len(verbs) < 4:
        return 10.0, "Too few bullets to judge verb variety.", []
    top, n = Counter(verbs).most_common(1)[0]
    ratio = n / float(len(verbs))
    return (_score_ratio(ratio, 0.15, 0.45),
            "Your most repeated opener is '%s' (%d of %d bullets)." % (top, n, len(verbs)),
            [])


def _check_tense(items, _level):
    """Gerund openers (Managing…) read as a job description. A current role legitimately uses
    present tense, so a quarter of the résumé is allowed to before this starts costing points."""
    if not items:
        return 0.0, "No experience bullets found to check.", []
    gerund = [i for i in items if _first_word(i["text"]).endswith("ing")]
    ratio = len(gerund) / float(len(items))
    return (_score_ratio(ratio, 0.0, 0.2),
            "%d of %d bullets open in the -ing form." % (len(gerund), len(items)),
            _clip([i["text"] for i in gerund]), [_first_word_span(i) for i in gerund])


def _check_bullet_length(items, _level):
    if not items:
        return 0.0, "No experience bullets found to check.", []
    long_ones = [i for i in items if len(i["text"]) > 220]
    thin = [i for i in items if len(i["text"]) < 40]
    ratio = (len(long_ones) + len(thin)) / float(len(items))
    detail = "%d over two lines, %d too thin, of %d." % (len(long_ones), len(thin), len(items))
    return (_score_ratio(ratio, 0.0, 0.2), detail,
            _clip([i["text"] for i in long_ones + thin]), _spans(long_ones + thin))


# Compiled once. These run on every scoring call and the alternations are large.
_FILLER_RE = _boundary_alt(_FILLER)
_ADVERBS_RE = _boundary_alt(sorted(_ADVERBS))
_PRONOUNS_RE = _boundary_alt(sorted(_PRONOUNS))


def _lexicon_check(text, rx, distinct_bad, noun, fix_hint=""):
    """Score on DISTINCT terms, highlight EVERY occurrence.

    The split is deliberate. Scoring on occurrences would mean one word repeated four times reads as
    four separate problems and tanks the check, when it is one habit to fix — and it would silently
    move thresholds that were calibrated against distinct counts. The viewer has the opposite need:
    marking only the first "successfully" leaves the other three looking approved.
    """
    spans, hits = _re_spans(rx, text)
    distinct = sorted(set(h.lower() for h in hits))
    if not distinct:
        return 10.0, "No %s found." % noun, [], []
    detail = "%d %s found" % (len(distinct), noun)
    if len(hits) != len(distinct):
        detail += " (%d occurrences)" % len(hits)
    return (_score_ratio(len(distinct), 0, distinct_bad), detail + "." + fix_hint,
            distinct[:MAX_OFFENDERS], spans)


def _check_filler(text, _level):
    return _lexicon_check(text, _FILLER_RE, 3, "filler phrase(s) / cliché(s)")


def _check_adverbs(text, _level):
    return _lexicon_check(text, _ADVERBS_RE, 3, "vague adverb(s)", " Quantify instead.")


def _check_pronouns(text, _level):
    return _lexicon_check(text, _PRONOUNS_RE, 2, "personal pronoun(s)")


def _check_passive(items, _level):
    if not items:
        return 0.0, "No experience bullets found to check.", []
    pat = re.compile(r"\b(?:was|were|been|being|is|are)\s+\w+(?:ed|en)\b", re.I)
    hits, spans = [], []
    for i in items:
        m = pat.search(i["text"])
        if not m:
            continue
        hits.append(i["text"])
        # The match, not the bullet: "was cut" is the defect, and the rest of the line may be fine.
        spans.append((i["start"] + m.start(), i["start"] + m.end()))
    ratio = len(hits) / float(len(items))
    return (_score_ratio(ratio, 0.0, 0.1),
            "%d of %d bullets use passive voice." % (len(hits), len(items)),
            _clip(hits), spans)


def _check_length(text, level):
    pages = max(1, int(round(len(text or "") / float(PAGE_CHARS))))
    budget = 1 if level == "entry" else 2
    if pages <= budget:
        return 10.0, "About %d page(s) — within the %d-page budget." % (pages, budget), []
    return (_score_ratio(pages - budget, 0, 2),
            "About %d page(s); %s should fit %d." % (pages, level, budget), [])


def _check_sections(sections, _level):
    groups = set(s["group"] for s in sections)
    want = ("experience", "education", "skills")
    missing = [w for w in want if w not in groups]
    return (_score_ratio(len(missing), 0, 3),
            "Missing section(s): %s." % ", ".join(missing) if missing
            else "Experience, education and skills all detected.",
            missing)


def _check_contact(header, text, _level):
    """Contact details must be in the HEADER. An email a parser only finds on page two is an
    email the ATS attaches to nothing."""
    head = "\n".join(header)
    found, missing = [], []
    for label, rx, where in (("email", _EMAIL_RE, head), ("phone", _PHONE_RE, head),
                             ("location", _LOCATION_RE, head), ("LinkedIn", _LINKEDIN_RE, text)):
        (found if rx.search(where or "") else missing).append(label)
    # LinkedIn is a nice-to-have; the first three are not.
    hard = [m for m in missing if m != "LinkedIn"]
    score = _score_ratio(len(hard), 0, 3)
    if missing and not hard:
        score = min(score, 9.0)
    return (score,
            "Header is missing: %s." % ", ".join(missing) if missing
            else "Email, phone, location and LinkedIn all present.",
            missing)


def _check_dates(exp_text, full_text, _level):
    """Judged on the experience block only. A degree line carrying a bare graduation year next to
    month-year employment dates is normal, not an inconsistency — scoring the whole document
    flagged almost every résumé for something no recruiter would call a defect."""
    present = _date_formats_in(exp_text)
    if not present:
        if _date_formats_in(full_text):
            return 3.0, "No dates in your experience section — an ATS builds work history from " \
                        "those, and without them it may read you as unemployed.", []
        return 0.0, "No dates detected — an ATS cannot build your work history.", []
    if len(present) == 1:
        return 10.0, "Experience dates use one consistent format (%s)." % present[0], []
    return (_score_ratio(len(present), 1, 4),
            "Experience dates mix %d formats: %s." % (len(present), ", ".join(present)),
            present)


def _check_unnecessary(text, sections, _level):
    bad = []
    if any(s["group"] == "summary" and "objective" in s["title"].lower() for s in sections):
        bad.append("Objective section")
    if _REFERENCES_RE.search(text or ""):
        bad.append("References line")
    if _PERSONAL_INFO_RE.search(text or ""):
        bad.append("Personal details (age / marital status / nationality)")
    return (_score_ratio(len(bad), 0, 3),
            "Remove: %s." % "; ".join(bad) if bad else "Nothing extraneous found.", bad)


def _check_leadership(items, sections, level):
    """Leadership counted as DEMONSTRATED, never as listed. A skills section containing
    'leadership' scores nothing here — recruiters read a bare claim as filler, and this rubric
    should not reward the thing it is trying to discourage."""
    if not items:
        return 0.0, "No experience bullets found to check.", []
    shown = [i["text"] for i in items
             if _first_word(i["text"]) in _LEAD_VERBS or _SCOPE_RE.search(i["text"])]
    ratio = len(shown) / float(len(items))
    good = {"entry": 0.25, "mid": 0.4, "senior": 0.55}[level]
    score = _score_ratio(ratio, good, 0.0)
    listed = [s["title"] for s in sections if s["group"] == "skills"
              and re.search(r"\blead(?:ership)?\b|\bteamwork\b", " ".join(
                  i["text"] for i in s["items"]), re.I)]
    detail = ("%d of %d bullets show leadership or scope (target %.0f%%)."
              % (len(shown), len(items), good * 100))
    if listed:
        score = max(0.0, score - 1.5)
        detail += " Leadership/teamwork is also LISTED in your skills — demonstrate it instead."
    # The offenders are the bullets WITHOUT scope, not the ones with it. Listing the successes
    # under a fix that says "show scope" reads as if the good lines were the problem.
    lacking = [i["text"] for i in items
               if _first_word(i["text"]) not in _LEAD_VERBS and not _SCOPE_RE.search(i["text"])]
    return round(score, 1), detail, _clip(lacking)


def _check_parse_health(text, sections, found_exp, _level):
    """A proxy for the damage a two-column layout does. When extraction interleaves columns you
    get many short fragments and few recognisable sections — the same signature as a résumé an
    ATS will scramble. This is the one check that says 'the file is the problem, not the words'."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return 0.0, "Nothing readable was extracted.", []
    frag = sum(1 for l in lines if len(l) < 25) / float(len(lines))
    problems = []
    if not found_exp:
        problems.append("no experience section recognised")
    if len(sections) < 3:
        problems.append("only %d section(s) detected" % len(sections))
    if frag > 0.45:
        problems.append("%.0f%% of lines are short fragments" % (frag * 100))
    score = 10.0 - 3.0 * len(problems)
    return (max(0.0, round(score, 1)),
            "Parsed cleanly: %d sections, %.0f%% fragment lines." % (len(sections), frag * 100)
            if not problems else "Parse risk: " + "; ".join(problems) + ".",
            problems)


# ------------------------------------------------------- mechanical consistency (the proofread half)
def _check_punctuation(sections, _level):
    """Bullets must end consistently — all with periods or none. Either convention is fine; mixing
    them is the thing a recruiter notices without being able to say why."""
    bullets = [i for s in sections for i in s["items"] if i["bullet"] and len(i["text"]) > 12]
    if len(bullets) < 3:
        return None, "Too few bullets to judge punctuation.", [], []
    with_dot = [i for i in bullets if i["text"].rstrip().endswith(".")]
    without = [i for i in bullets if not i["text"].rstrip().endswith(".")]
    minority = with_dot if len(with_dot) <= len(without) else without
    ratio = len(minority) / float(len(bullets))
    if not minority:
        return 10.0, "All %d bullets end consistently." % len(bullets), [], []
    return (_score_ratio(ratio, 0.0, 0.35),
            "%d of %d bullets break your own end-punctuation convention."
            % (len(minority), len(bullets)),
            _clip([i["text"] for i in minority]),
            # The last character, not the line: the defect is a present or missing full stop.
            [(i["end"] - 1, i["end"]) for i in minority])


def _casing_of(s):
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return ""
    if s == s.upper():
        return "UPPER"
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]*", s)]
    if words and all(w[0].isupper() for w in words):
        return "Title"
    return "Sentence"


def _check_capitalization(sections, _level):
    """Section headings in one casing convention. Three EXPERIENCE / Education / skills headings in
    the same document is the clearest signal a résumé was assembled from two templates."""
    titled = [s for s in sections if s["title"]]
    if len(titled) < 2:
        return None, "Too few headings to judge capitalisation.", [], []
    kinds = {}
    for s in titled:
        kinds.setdefault(_casing_of(s["title"]), []).append(s)
    if len(kinds) <= 1:
        return (10.0, "All %d headings use one convention (%s case)."
                % (len(titled), list(kinds)[0] or "n/a"), [], [])
    winner = max(kinds, key=lambda k: len(kinds[k]))
    odd = [s for k, v in kinds.items() if k != winner for s in v]
    return (_score_ratio(len(odd), 0, 3),
            "Headings mix %d capitalisation styles; %d differ from the rest."
            % (len(kinds), len(odd)),
            [s["title"] for s in odd][:MAX_OFFENDERS],
            [(s["start"], s["end"]) for s in odd])


# Two spaces mid-line, or a space before closing punctuation. Leading indentation is deliberately
# excluded — plenty of résumés indent, and flagging that would bury the real edits.
_DOUBLE_SPACE_RE = re.compile(r"(?<=\S)[ \t]{2,}(?=\S)")
# Explicit space-and-tab rather than \s+ on purpose: \s matches a newline, so the span could
# straddle a line break and the viewer would draw a highlight across two lines for a
# one-character defect.
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t]+[,.;:!?](?=\s|$)")


def _check_spacing(text, _level):
    """Spacing debris — the fingerprints of editing. Cheap to fix and free to detect."""
    spans, hits = [], []
    for rx, label in ((_DOUBLE_SPACE_RE, "double space"),
                      (_SPACE_BEFORE_PUNCT_RE, "space before punctuation")):
        s, h = _re_spans(rx, text)
        spans += s
        if h:
            hits.append("%d x %s" % (len(h), label))
    if not spans:
        return 10.0, "No stray spacing.", [], []
    return (_score_ratio(len(spans), 0, 4),
            "%d spacing problem(s): %s." % (len(spans), ", ".join(hits)),
            hits, spans)


_TRIGRAM_STOP = frozenset(("and", "the", "of", "to", "for", "in", "with", "a", "an", "on", "at"))


def _check_repeated_phrases(items, _level):
    """Repeated three-word phrases anywhere, not just repeated opening verbs.

    The opener check already catches "Led ... Led ... Led". This catches the subtler version, where
    the same construction ("responsible for the", "worked closely with") is reused down the page and
    makes four different jobs read like one.
    """
    if len(items) < 3:
        return None, "Too few bullets to judge repetition.", [], []
    seen = {}
    for i in items:
        words = re.findall(r"[A-Za-z][A-Za-z'-]*", i["text"].lower())
        for n in range(len(words) - 2):
            tri = tuple(words[n:n + 3])
            if all(w in _TRIGRAM_STOP for w in tri):
                continue
            seen.setdefault(" ".join(tri), []).append(i)
    repeats = {p: v for p, v in seen.items() if len(v) > 1}
    if not repeats:
        return 10.0, "No repeated phrases.", [], []
    worst = sorted(repeats.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    # Spans are resolved per ITEM rather than over the whole document: the trigrams were built from
    # item text, so that is the only string their word order is guaranteed to appear in, and the
    # item carries the offset needed to make the position absolute.
    spans = []
    for phrase, owners in worst[:12]:
        rx = _boundary_alt([phrase])
        for i in owners:
            for m in rx.finditer(i["text"]):
                spans.append((i["start"] + m.start(), i["start"] + m.end()))
    return (_score_ratio(len(repeats), 0, 5),
            "%d phrase(s) repeat across bullets; the most reused is “%s” (%d times)."
            % (len(repeats), worst[0][0], len(worst[0][1])),
            ["%s (x%d)" % (p, len(v)) for p, v in worst[:MAX_OFFENDERS]],
            spans)


def _check_skills_demonstrated(sections, items, _level):
    """A skill in your Skills list should be visible in an accomplishment.

    This generalises the leadership rule. Recruiters read a bare list as a claim; the same word
    inside a bullet is evidence. Only flags listed skills that appear NOWHERE in the experience
    text, so a résumé is never penalised for also summarising what it demonstrates.
    """
    skill_secs = [s for s in sections if s["group"] == "skills"]
    if not skill_secs or not items:
        return None, "No skills section to cross-check.", [], []
    body = " ".join(i["text"] for i in items).lower()
    listed, spans = [], []
    for s in skill_secs:
        for it in s["items"]:
            for m in re.finditer(r"[A-Za-z][A-Za-z0-9+#.'/ -]{1,28}", it["text"]):
                tok = m.group(0).strip(" -/.")
                if len(tok) < 3 or tok.lower() in _TRIGRAM_STOP:
                    continue
                if tok.lower() in body:
                    continue
                listed.append(tok)
                spans.append((it["start"] + m.start(), it["start"] + m.start() + len(tok)))
    if not listed:
        return 10.0, "Every listed skill also appears in your experience.", [], []
    return (_score_ratio(len(listed), 0, 10),
            "%d listed skill(s) never appear in an accomplishment." % len(listed),
            listed[:MAX_OFFENDERS], spans)


# ------------------------------------------------------------------------------------- spelling
# The vocabulary is mined from our own 23k job descriptions by scripts/build_resume_vocab.py, not
# from a generic English dictionary, and that is the whole point: the words a general dictionary
# false-positives on — Kubernetes, Jaggaer, IntelliBuy, Workday — are the words that saturate a
# corpus of real postings. It also means no new dependency and nothing to download.
#
# Dormant if the file is missing, which is the same contract core.load_sponsor_counts already has:
# an absent data file costs the feature, never the page.
VOCAB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resume_vocab.json")
_vocab_cache = {"words": None, "loaded": False}


def load_vocab(path=VOCAB_PATH):
    if not _vocab_cache["loaded"]:
        _vocab_cache["loaded"] = True
        try:
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
            words = blob.get("words") if isinstance(blob, dict) else blob
            _vocab_cache["words"] = frozenset(w.lower() for w in (words or ()))
        except Exception:
            _vocab_cache["words"] = None
    return _vocab_cache["words"]


def _reset_vocab_cache():
    """For tests and long-lived workers, mirroring core._reset_idf_cache."""
    _vocab_cache.update({"words": None, "loaded": False})


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]{2,}")
# Deliberately never flagged, whatever the corpus says.
_SPELL_SKIP = frozenset(("resume", "cv", "curriculum", "vitae", "linkedin", "github"))


_ALPHA = "abcdefghijklmnopqrstuvwxyz"


def _nearest(word, vocab):
    """A known word one edit away from `word`, or "". Classic edits-1 over the vocabulary.

    THIS is what makes the check usable. "Absent from the vocabulary" on its own flagged five real
    words per résumé and caught nothing — surnames, employers and schools (hada, entegris,
    medi-caps), plus ordinary English the job corpus simply never uses (bootstrapped, reinvesting,
    astrology). A 22k-word corpus is narrower than a dictionary, so absence is weak evidence.

    Being one edit from a known word is strong evidence: "recieved" reaches "received",
    "budgett" reaches "budget", "reprot" reaches "report" — while "astrology" and "entegris" reach
    nothing, because they are not typos of anything. It also yields the correction, which turns the
    finding from an accusation into a fix.
    """
    if len(word) < 4:
        return ""
    found = set()
    for i in range(len(word) + 1):
        a, b = word[:i], word[i:]
        if b and (a + b[1:]) in vocab:                         # deletion
            found.add(a + b[1:])
        if len(b) > 1 and (a + b[1] + b[0] + b[2:]) in vocab:  # transposition
            found.add(a + b[1] + b[0] + b[2:])
        for c in _ALPHA:
            if b and (a + c + b[1:]) in vocab:                 # substitution
                found.add(a + c + b[1:])
            if (a + c + b) in vocab:                           # insertion
                found.add(a + c + b)
    if not found:
        return ""
    # Prefer a candidate that keeps the first letter, then the longest shared prefix. Returning the
    # first hit instead suggested "anual → manual" over "annual": both are one insertion, and edit
    # distance alone cannot tell them apart. A wrong suggestion beside a correct flag still reads as
    # the tool being wrong.
    def rank(c):
        shared = 0
        for x, y in zip(word, c):
            if x != y:
                break
            shared += 1
        return (c[0] != word[0], -shared, abs(len(c) - len(word)), c)

    return sorted(found, key=rank)[0]


def _check_spelling(text, _level):
    vocab = load_vocab()
    if not vocab:
        return None, "Spelling check is dormant (no vocabulary file built).", [], []
    _, sections = split_sections(text)
    head_end = sections[0]["start"] if sections else 0
    spans, pairs = [], {}
    for line, line_start in _lines_with_offsets(text):
        for m in _WORD_RE.finditer(line):
            abs_start = line_start + m.start()
            # The header holds the name, the email and often an employer — the densest concentration
            # of words no vocabulary contains, and none of them are typos worth reporting.
            if abs_start < head_end:
                continue
            tok = m.group(0)
            low = tok.lower().strip("'’-")
            if len(low) < 4 or low in vocab or low in _SPELL_SKIP:
                continue
            # Capitalised anywhere = proper noun. Deliberately unconditional: gating it on "not
            # first on the line" left every name-led line and bullet-led product name exposed,
            # which was most of the false positives. A capitalised typo now slips through, and that
            # is the right trade — one miss costs nothing, one false accusation costs the report.
            if tok[0].isupper() or tok.isupper() or any(ch.isdigit() for ch in tok):
                continue
            if "-" in tok and all(p.lower() in vocab for p in tok.split("-") if len(p) > 2):
                continue          # hyphenated compound of known words
            fix = _nearest(low, vocab)
            if not fix:
                continue          # absent, but not a typo of anything we know
            pairs[low] = fix
            spans.append((abs_start, line_start + m.end()))
    if not pairs:
        return 10.0, "No misspellings found.", [], []
    shown = ["%s → %s" % (w, f) for w, f in sorted(pairs.items())]
    return (_score_ratio(len(pairs), 0, 3),
            "%d likely misspelling(s): %s." % (len(pairs), ", ".join(shown[:4])),
            shown[:MAX_OFFENDERS], spans)


def _check_role_keywords(text, _sections, _items, _level):
    """Coverage of the skills your track's postings actually ask for. See resume_keywords.py.

    No spans. The complaint here is about terms that are ABSENT, and there is nothing in the
    document to point at; the Keywords tab lists have/missing as chips instead. Highlighting the
    matched terms was the alternative and was rejected — marking correct words while the user has
    clicked a list of problems reads as if those words were the problem.
    """
    try:
        import resume_keywords
        # Evidence is the experience bullets: a keyword has to be earned in an accomplishment, not
        # pasted into a skills line. Track inference still sees the whole document.
        ev = resume_keywords.evaluate(text, evidence_text=" ".join(i["text"] for i in _items))
    except Exception:
        return None, "Keyword expectations unavailable.", [], []
    if not ev.get("total"):
        return None, "No keyword expectations available.", [], []
    ratio = len(ev["have"]) / float(ev["total"])
    src = "your job corpus" if ev["source"] == "corpus" else "the curated skill list"
    # 35% of the top expectations is the bar, not 50%: nobody legitimately has every commonly-asked
    # skill in a track, and a target no real résumé can hit is a target that teaches nothing.
    return (_score_ratio(ratio, 0.6, 0.05),
            "Your bullets show %d of the %d hard skills %s-track postings ask for most (from %s)."
            % (len(ev["have"]), ev["total"], ev["track"], src),
            ev["missing"][:MAX_OFFENDERS], [])


_LABELS = {
    "quantified_impact": "Quantified achievements",
    "weak_verb_openers": "Weak verb openers",
    "action_verb_openers": "Action-verb openers",
    "prose_not_bullets": "Bullets, not paragraphs",
    "verb_variety": "Verb variety",
    "tense_consistency": "Verb tense",
    "bullet_length": "Bullet length",
    "filler_buzzwords": "Filler and clichés",
    "adverbs": "Vague adverbs",
    "personal_pronouns": "Personal pronouns",
    "passive_voice": "Passive voice",
    "resume_length": "Résumé length",
    "sections_present": "Expected sections",
    "contact_details": "Contact details",
    "date_consistency": "Date formats",
    "unnecessary_content": "Extraneous content",
    "leadership_signals": "Leadership demonstrated",
    "parse_health": "ATS parse health",
    "punctuation_consistency": "End punctuation",
    "capitalization_consistency": "Heading capitalisation",
    "spacing_hygiene": "Spacing",
    "repeated_phrases": "Repeated phrases",
    "spelling": "Spelling",
    "skills_demonstrated": "Skills backed by evidence",
    "role_keywords": "Keywords your roles expect",
}
_FIXES = {
    "quantified_impact": "Add a number to every bullet that makes a claim without one — %, $, "
                         "headcount, hours saved, scale.",
    "weak_verb_openers": "Replace Assisted / Helped / Responsible for with the verb for what you "
                         "actually did: led, built, cut, shipped.",
    "action_verb_openers": "Start each bullet with a past-tense action verb, not a noun phrase.",
    "prose_not_bullets": "Break the paragraph into one-line bullets, one accomplishment each.",
    "verb_variety": "Vary your openers; repeating one verb flattens everything into one job.",
    "tense_consistency": "Use past tense throughout; keep present tense to your current role.",
    "bullet_length": "Keep bullets to one or two lines. Split the long ones, merge the thin ones.",
    "filler_buzzwords": "Cut the phrase and state the outcome instead.",
    "adverbs": "Delete the adverb and put a number in its place.",
    "personal_pronouns": "Drop I / we — start at the verb.",
    "passive_voice": "Rewrite so you are the subject: 'Cut cost 18%', not 'Cost was cut'.",
    "resume_length": "Cut the oldest and least relevant roles first.",
    "sections_present": "Add the missing section under a standard, literal heading.",
    "contact_details": "Put name, email, phone and city/state in the header, as plain text.",
    "date_consistency": "Pick one date format and use it everywhere.",
    "unnecessary_content": "Delete it — it costs a line and gives a recruiter nothing.",
    "leadership_signals": "Show scope: who you led, how many, what changed because of it.",
    "parse_health": "Move to a single-column layout with no tables, images or text boxes, and "
                    "re-export. Then re-score to confirm the parse improved.",
    "punctuation_consistency": "Pick one — periods on every bullet or none — and apply it "
                               "throughout.",
    "capitalization_consistency": "Put every section heading in the same case.",
    "spacing_hygiene": "Delete the double spaces and the spaces before punctuation.",
    "repeated_phrases": "Rewrite the reused phrase so each role reads as its own job.",
    "spelling": "Check each word. Names, tools and acronyms are never flagged, so these are "
                "likely real.",
    "skills_demonstrated": "Either show the skill in a bullet or drop it from the list — a "
                           "recruiter reads an unevidenced list as filler.",
    "role_keywords": "Work the missing terms into bullets where they are true of you. Do not "
                     "paste them into a skills list.",
}


# ---------------------------------------------------------------------------------------------
# SPECIFIC advice.
#
# _FIXES above is the RULE. It is true of every résumé and therefore about nobody's: a user told
# "Add a number to every bullet that makes a claim without one" has been handed the principle and
# still has to find the bullets, decide which number, and write the line. Twenty-five sentences
# like that, identical for every user, is what makes a résumé tool feel like a form letter.
#
# Every check already collects its `offenders`. This layer spends them, so the primary sentence
# names the user's own line and the rule drops to a second, quieter one.
#
# Rules for anything added here:
#   * Quote the user, never paraphrase. A paraphrase is how they find out we did not read it.
#   * One instruction, not a menu — the worst offender only. The rest are marked in the document.
#   * Return None rather than a vague sentence. Falling back to the rule beats faking specificity.
#   * Ask a question where a question is the actual next step ("how many reports?"). Answering it
#     is the edit.
# ---------------------------------------------------------------------------------------------
def _q(s, n=64):
    """The user's own words, quoted, short enough to scan and long enough to locate the line."""
    s = " ".join((s or "").split())
    return "“%s”" % (s if len(s) <= n else s[:n - 1].rstrip() + "…")


def _join(xs, last="and", cap=3):
    xs = [x for x in (xs or []) if x][:cap]
    if not xs:
        return ""
    if len(xs) == 1:
        return xs[0]
    return ", ".join(xs[:-1]) + " " + last + " " + xs[-1]


def _sf_quantified(off, detail):
    import resume_bullets                      # local: resume_bullets imports THIS module
    b = off[0]
    metrics = resume_bullets.metrics_for(b)
    if not metrics:
        return None
    return "Start with %s. Ask it one question: %s?" % (_q(b), metrics[0])


def _sf_weak_openers(off, detail):
    import resume_bullets
    b = off[0]
    swaps = resume_bullets.swaps_for(b)[:3]
    if not swaps:
        return None
    return ("%s opens on “%s”, which describes the job you were given rather than the "
            "work you did. %s each say something that one cannot."
            % (_q(b), _first_word(b).title(), _join(swaps)))


def _sf_action_openers(off, detail):
    return ("%s has no verb in it — it names a thing, so a reader has to guess what you did with "
            "it. Say what you did." % _q(off[0]))


def _sf_filler(off, detail):
    return ("%s is the phrase to cut first. It survives in a résumé because it sounds like "
            "content; delete it and write what actually happened." % _q(off[0]))


def _sf_adverbs(off, detail):
    return ("%s tells a recruiter nothing they were not already assuming. Delete it, or replace "
            "it with the number that would have proved it." % _q(off[0]))


def _sf_pronouns(off, detail):
    # Offenders arrive lower-cased from the token scan, and rendering a bare “i” back at the user
    # reads as our typo rather than their word.
    word = "I" if (off[0] or "").lower() == "i" else off[0]
    return ("Drop %s — the whole document is understood to be about you, so the word is spent "
            "before it is read. Start at the verb." % _q(word))


def _sf_passive(off, detail):
    return ("%s puts the outcome in front and leaves you out of it. Recast it so you are the "
            "subject of the sentence." % _q(off[0]))


def _sf_leadership(off, detail):
    return ("%s does not say what you were responsible for. Add the scope — how many people, how "
            "big the thing was, or what changed because it was yours." % _q(off[0]))


def _sf_bullet_length(off, detail):
    b = off[0]
    if len(b) > 220:
        return ("%s runs past two lines. Cut the setup and keep the outcome — the first half is "
                "context a reader will infer." % _q(b))
    return ("%s is too thin to carry an achievement. Either say what it produced, or fold it into "
            "the bullet above." % _q(b))


def _sf_contact(off, detail):
    return ("Add %s to the header, as plain text on one line. A parser reads the top of the page "
            "first and gives up fast." % _join(off, "and", 4))


def _sf_unnecessary(off, detail):
    listed = _join(off, "and", 3).lower()
    if len(off) == 1:
        return ("Delete the %s. It costs you a line and tells a recruiter something they had "
                "already assumed." % listed)
    return ("Delete the %s. They cost you a line each and tell a recruiter nothing they had not "
            "already assumed." % listed)


def _sf_dates(off, detail):
    if len(off) < 2:
        return None
    return ("Your experience dates mix %s. Pick the one you use most and rewrite the others to "
            "match — an ATS builds your work history from these." % _join(off, "and", 4))


def _sf_skills_demonstrated(off, detail):
    return ("%s %s in your skills list but never in an accomplishment. Show %s in a bullet, or "
            "drop it — an unevidenced list is the pattern recruiters read as filler."
            % (_join(off, "and", 3), "appears" if len(off) == 1 else "appear",
               "it" if len(off) == 1 else "one of them"))


def _sf_role_keywords(off, detail):
    one = len(off) == 1
    return ("Your track's postings ask most often for %s, and %s nowhere in your bullets. Work in "
            "whichever are genuinely true of you — in a bullet, not in a skills list."
            % (_join(list(off), "and", 3), "it appears" if one else "they appear"))


def _sf_capitalization(off, detail):
    return ("%s is cased differently from your other headings. Match it to the rest — a parser "
            "uses headings to find your sections." % _q(off[0]))


def _sf_repeated_phrases(off, detail):
    return ("%s is doing duty in more than one role. Rewrite the later ones so each job reads as "
            "its own job." % _q(off[0]))


def _sf_spelling(off, detail):
    first = (off[0] or "").split("→")
    if len(first) != 2:
        return None
    return ("%s is one edit away from “%s”. Only near-misses are flagged, never names or "
            "acronyms, so this is very likely a real typo."
            % (_q(first[0].strip()), first[1].strip()))


# key -> builder. Absent keys, and any builder returning None, fall back to the rule in _FIXES.
# Deliberately absent: prose_not_bullets, tense_consistency, verb_variety, repeated openers,
# parse_health, punctuation_consistency, spacing_hygiene, resume_length, sections_present. Each of
# those already states its own numbers in `detail` ("your most repeated opener is 'Managed', 6 of
# 15"), and a second sentence restating them in a different voice is noise, not specificity.
_SPECIFIC = {
    "quantified_impact": _sf_quantified,
    "weak_verb_openers": _sf_weak_openers,
    "action_verb_openers": _sf_action_openers,
    "filler_buzzwords": _sf_filler,
    "adverbs": _sf_adverbs,
    "personal_pronouns": _sf_pronouns,
    "passive_voice": _sf_passive,
    "leadership_signals": _sf_leadership,
    "bullet_length": _sf_bullet_length,
    "contact_details": _sf_contact,
    "unnecessary_content": _sf_unnecessary,
    "date_consistency": _sf_dates,
    "skills_demonstrated": _sf_skills_demonstrated,
    "role_keywords": _sf_role_keywords,
    "capitalization_consistency": _sf_capitalization,
    "repeated_phrases": _sf_repeated_phrases,
    "spelling": _sf_spelling,
}


# Checks the Bullets tab covers line by line. The Review tab names the worst offender and then
# hands off, rather than duplicating a work queue that already exists.
_BULLET_LEVEL = frozenset((
    "quantified_impact", "weak_verb_openers", "action_verb_openers", "bullet_length",
    "passive_voice", "leadership_signals", "verb_variety", "tense_consistency",
))


def specific_fix(key, offenders, detail):
    """The instruction for THIS résumé, or None to fall back to _FIXES[key].

    Wrapped in a blanket except on purpose: an advice string is decoration on a score, and a
    KeyError raised while phrasing a suggestion must never cost the user their report.
    """
    fn = _SPECIFIC.get(key)
    if not fn or not offenders:
        return None
    try:
        return fn(list(offenders), detail)
    except Exception:
        return None


def score_resume(text, level="mid"):
    """Grade `text` and return the full report.

    {score, band, level, checks:[…], groups:[…], parse:{…}, top_fixes:[…]}

    Never raises: an empty or unreadable résumé scores 0 with `parse.readable` False, because the
    caller's job is to show the user why, not to handle an exception.
    """
    level = level if level in LEVELS else "mid"
    text = text or ""
    header, sections = split_sections(text)
    items, found_exp = _experience_items(sections)

    raw = {
        "quantified_impact":   _check_quantified(items, level),
        "weak_verb_openers":   _check_weak_openers(items, level),
        "action_verb_openers": _check_action_openers(items, level),
        "prose_not_bullets":   _check_prose(sections, level),
        "verb_variety":        _check_verb_variety(items, level),
        "tense_consistency":   _check_tense(items, level),
        "bullet_length":       _check_bullet_length(items, level),
        "filler_buzzwords":    _check_filler(text, level),
        "adverbs":             _check_adverbs(text, level),
        "personal_pronouns":   _check_pronouns(text, level),
        "passive_voice":       _check_passive(items, level),
        "resume_length":       _check_length(text, level),
        "sections_present":    _check_sections(sections, level),
        "contact_details":     _check_contact(header, text, level),
        "date_consistency":    _check_dates(_experience_text(sections), text, level),
        "unnecessary_content": _check_unnecessary(text, sections, level),
        "leadership_signals":  _check_leadership(items, sections, level),
        "parse_health":        _check_parse_health(text, sections, found_exp, level),
        "punctuation_consistency":    _check_punctuation(sections, level),
        "capitalization_consistency": _check_capitalization(sections, level),
        "spacing_hygiene":            _check_spacing(text, level),
        "repeated_phrases":           _check_repeated_phrases(items, level),
        "skills_demonstrated":        _check_skills_demonstrated(sections, items, level),
        "role_keywords":              _check_role_keywords(text, sections, items, level),
        "spelling":                   _check_spelling(text, level),
    }

    # A check may return score None to mean "not applicable here" — no vocabulary file built, no
    # skills section to cross-check. Those are EXCLUDED from the denominator rather than awarded
    # 10/10, because awarding full marks for a check that never ran hands out free points and
    # inflates the score of exactly the résumés we know least about.
    dormant = {k for k, res in raw.items() if res[0] is None}
    total_w = sum(_weight(k, level) for k in raw if k not in dormant)
    checks, earned = [], 0.0
    for key, res in raw.items():
        # Checks return (score, detail, offenders) or (score, detail, offenders, spans). Tolerating
        # both lets a check gain highlight support without every other one having to change on the
        # same day, and a check with nothing locatable to point at legitimately has no spans.
        sc, detail, offenders = res[0], res[1], res[2]
        spans = list(res[3]) if len(res) > 3 else []
        w = 0.0 if key in dormant else _weight(key, level)
        if key not in dormant:
            earned += sc * w
        checks.append({
            "key": key, "label": _LABELS[key], "group": _GROUPS[key],
            "score": None if key in dormant else round(sc, 1),
            "weight": w, "detail": detail, "dormant": key in dormant,
            "offenders": offenders, "spans": spans,
            # `fix` names the user's own line where we can build that sentence; `rule` is the
            # general principle, always present. The panel leads with fix and demotes rule, so a
            # check with nothing quotable degrades to what it always said rather than to nothing.
            "fix": specific_fix(key, offenders, detail) or _FIXES[key],
            "rule": _FIXES[key],
            "bullet_level": key in _BULLET_LEVEL,
            # Points of the final 100 this check is currently costing — the only ordering that
            # answers "what should I fix first?" without the user doing the arithmetic.
            "points_lost": 0.0 if key in dormant or not total_w
            else round((10.0 - sc) * w / total_w * 10, 1),
        })

    score = int(earned / total_w * 10) if total_w else 0
    readable = len(text.strip()) >= 40

    groups = []
    for name in GROUP_ORDER:
        rows = [c for c in checks if c["group"] == name and not c["dormant"]]
        gw = sum(c["weight"] for c in rows)
        groups.append({"name": name,
                       "score": int(sum(c["score"] * c["weight"] for c in rows) / gw * 10)
                       if gw else 0})

    # The gate. Reads the quantified_impact check itself; a dormant one (no bullets to judge) leaves
    # the ceiling at 100 rather than punishing a résumé for a parse failure.
    q = next((c for c in checks if c["key"] == "quantified_impact"), None)
    ceiling = 100.0 if (q is None or q["dormant"]) else (
        IMPACT_GATE_FLOOR + IMPACT_GATE_SLOPE * q["score"])
    capped = int(min(score, ceiling))
    gated = capped < score
    score = capped

    return {
        "score": score if readable else 0,
        "band": band_of(score if readable else 0),
        "band_note": band_note(band_of(score if readable else 0)),
        # Surfaced so the panel can say WHY a tidy résumé did not score well, rather than leaving the
        # user to diff the group tiles against the total and guess.
        "impact_gated": gated and readable,
        "impact_ceiling": int(ceiling),
        "level": level,
        "checks": sorted(checks, key=lambda c: (-c["points_lost"], c["label"])),
        "groups": groups,
        "top_fixes": [c for c in sorted(checks, key=lambda c: -c["points_lost"])
                      if c["points_lost"] >= 0.5][:5],
        "parse": parse_view(text),
    }


# Calibrated to THIS rubric and nothing else — not comparable to any other tool's number, and
# provisional until they can be fitted to real callback outcomes.
#
# Deliberately HARD. The first cut put every real résumé between 86 and 92, so the top band
# described the average document and the score carried almost no information. A grader whose highest
# band is where everyone lands is a compliment, not a measurement. Under the tightened thresholds a
# well-written résumé sits in the sixties or seventies, and 88+ means genuinely nothing left to fix.
_BANDS = ((88, "Exceptional"), (76, "Strong"), (62, "Solid"), (45, "Needs work"), (0, "Weak"))

# A band shown as a bare adjective is a verdict with no content: "Solid" told a user neither what
# the word meant on this scale nor what would move it. Each note says what the band IS and what the
# next move is, and says so in the same register as the rest of the panel -- no congratulation, no
# scolding. The floors are deliberately hard (see above), so most real résumés land in the middle
# two and the copy has to make that read as a position rather than a failure.
_BAND_NOTES = {
    "Exceptional": "Nothing structural left. The remaining points are taste, not defects.",
    "Strong": "Reads well and would survive a screen. The fixes below are refinements, not repairs.",
    "Solid": "The bones are right and the writing is not yet doing the work. Most résumés that "
             "get interviews sit here before someone rewrites the bullets.",
    "Needs work": "A recruiter would read this as a list of duties. Fix the top three below, in "
                  "order, and the number moves a long way.",
    "Weak": "This is a job description of your old roles, not a record of what you did. Start with "
            "the first fix below — it is worth more than the rest combined.",
}


def band_of(score):
    for floor, name in _BANDS:
        if score >= floor:
            return name
    return "Needs work"


def band_note(band):
    return _BAND_NOTES.get(band, "")


def annotate_html(text, checks, escape=None):
    """The résumé as reviewable HTML: every span from every check wrapped in a `<mark>` carrying the
    check keys that claimed it, all of them inert until the page activates one.

    Rendered ONCE, server-side, with nothing active. Clicking a check in the rail then only toggles
    an attribute on the document — no re-fetch, no per-click work, and the marks cannot drift out of
    step with the counts in the rail because they came from the same report.

    Overlaps are why this is a sweep and not string surgery. "Responsible for" is filler AND
    "Responsible" is a weak opener, so their spans genuinely overlap; nesting `<mark>` inside
    `<mark>` would make the inner one impossible to style independently. Instead the character range
    is cut at every span boundary and each resulting run names all the checks covering it.
    """
    text = text or ""
    if escape is None:
        import html as _html
        escape = _html.escape
    # Boundary sweep: at each position, which checks start and which end.
    starts, ends = {}, {}
    for c in checks:
        for (a, b) in c.get("spans") or ():
            if not (0 <= a < b <= len(text)):
                continue                      # a bad offset must not corrupt the document
            starts.setdefault(a, set()).add(c["key"])
            ends.setdefault(b, set()).add(c["key"])
    if not starts:
        return escape(text)
    cuts = sorted(set(starts) | set(ends) | {0, len(text)})
    out, active = [], set()
    for i, pos in enumerate(cuts):
        active -= ends.get(pos, set())
        active |= starts.get(pos, set())
        nxt = cuts[i + 1] if i + 1 < len(cuts) else len(text)
        if nxt <= pos:
            continue
        chunk = escape(text[pos:nxt])
        if active:
            out.append('<mark class="rv-hit" data-checks="%s">%s</mark>'
                       % (" ".join(sorted(active)), chunk))
        else:
            out.append(chunk)
    return "".join(out)


def parse_view(text):
    """What a parser actually extracts — the panel that shows the user their résumé as an ATS
    reads it. Cheap because the extraction already happened upstream in
    core.resume_text_from_upload; this only reports on it.
    """
    text = text or ""
    header, sections = split_sections(text)
    head = "\n".join(header)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    items, found_exp = _experience_items(sections)
    return {
        "readable": len(text.strip()) >= 40,
        "words": len(re.findall(r"[A-Za-z']+", text)),
        "pages": max(1, int(round(len(text) / float(PAGE_CHARS)))) if text else 0,
        "name": header[0] if header else "",
        "contact": {
            "email": _found(_EMAIL_RE, head),
            "phone": _found(_PHONE_RE, head),
            "location": _found(_LOCATION_RE, head),
            "linkedin": _found(_LINKEDIN_RE, text),
        },
        "sections": [{"title": s["title"], "group": s["group"], "items": len(s["items"])}
                     for s in sections],
        "bullets": sum(1 for s in sections for i in s["items"] if i["bullet"]),
        "experience_bullets": len(items),
        "found_experience_section": found_exp,
        "date_formats": _date_formats_in(_experience_text(sections)) or _date_formats_in(text),
        "fragment_pct": int(round(100.0 * sum(1 for l in lines if len(l) < 25) / len(lines)))
        if lines else 0,
    }
