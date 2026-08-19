"""
resume_brain/voice.py -- ONE definition of how our writing is supposed to sound.

Imported by every surface that either WRITES prose (the two AI prompts) or JUDGES it
(resume_score's filler check, the prompt evals). Before this module the two prompts each
carried their own half-sentence of style guidance, and in both cases that sentence was
"strong action verbs" -- which is the single instruction most likely to make a model reach for
"Spearheaded", "Leveraged" and "Orchestrated". Removing it is most of the fix; this file is
where the replacement lives so the two prompts cannot drift apart again.

Deliberately DEPENDENCY-FREE (no core, no db, no requests). `core.py` imports nothing else
from this package, so a self-contained module here is importable from both sides with no
cycle. Living inside resume_brain/ also means the deploy picks it up for free -- the package
is copied recursively by scripts/build_deploy_zip.py (DIRS) and ../.cpanel.yml (`cp -rf`),
unlike a new root-level module which has to be listed in both.

Sources, both permissively licensed and reimplemented rather than vendored:
  * cliche list and the specificity pairs -- santifer/career-ops `modes/_writing.md` (MIT)
  * intensity levels and preservation rules -- srbhr/Resume-Matcher
    `apps/backend/app/prompts/templates.py` (Apache-2.0)
"""

import re

# ---------------------------------------------------------------------------------------------
# SCORED filler. This tuple is the one resume_score._FILLER re-exports, and it is a verbatim
# move -- not one phrase added, removed or reworded. Anything new goes in AI_SLOP below. The
# bands are calibrated against this exact list (see resume_score._BANDS), so growing it
# silently re-scores every resume in the database.
# ---------------------------------------------------------------------------------------------
FILLER = (
    "team player", "hard worker", "hard working", "detail oriented", "detail-oriented",
    "self starter", "self-starter", "go getter", "go-getter", "think outside the box", "synergy",
    "results driven", "results-driven", "proven track record", "track record of", "dynamic",
    "passionate", "guru", "ninja", "rockstar", "rock star", "best of breed", "value add",
    "responsible for", "duties included", "various", "several", "numerous", "etc",
    "wide range of", "in charge of", "helped to", "worked closely with",
)

# ---------------------------------------------------------------------------------------------
# AI slop: what a language model reaches for when nobody tells it not to. NOT SCORED -- these
# constrain the prompts, and surface as an advisory (weight 0) check.
#
# The reason it has to stay advisory is concrete, not caution for its own sake: "spearheaded"
# is already in resume_score._LEAD_VERBS, where it EARNS credit under leadership_signals. Score
# it as filler too and the same word both helps and hurts, which is indefensible to a user
# reading two contradictory rows about one line. The same trap waits for "championed" and
# "drove". Decide a weight only after measuring the real corpus; until then a user gets the
# advice without the penalty.
# ---------------------------------------------------------------------------------------------
AI_SLOP = (
    "spearheaded", "leveraged", "leveraging", "facilitated", "utilized", "utilised",
    "orchestrated", "championed", "architected", "pioneered", "propelled",
    "passionate about", "results-oriented", "results oriented", "demonstrated ability",
    "extensive experience", "instrumental in", "tasked with",
    "synergies", "robust", "seamless", "seamlessly", "cutting-edge", "cutting edge",
    "innovative", "best practices", "best-in-class", "world-class", "state of the art",
    "fast-paced world", "ever-changing", "dynamic environment", "wide array of",
    "delve", "delved", "myriad", "plethora", "underscores", "testament to",
)

# Characters an ATS mangles. Kept as escapes so the list is unambiguous in a diff.
ATS_HOSTILE = ("—", "–", "‘", "’", "“", "”", "​")

# Weak-to-specific pairs. These do double duty: they teach the model what "be specific" means
# (an abstract instruction it will agree with and then ignore), and they set the house style.
SPECIFICITY_PAIRS = (
    ("improved performance", "Cut p95 latency from 2.1s to 380ms"),
    ("designed a scalable architecture", "Moved retrieval to Postgres + pgvector over 12k docs"),
    ("managed vendor relationships", "Ran 9 vendor contracts worth $2.4M"),
    ("streamlined the process", "Cut invoice approval from 6 days to 2"),
    ("supported the team", "Trained 4 analysts on the reconciliation workflow"),
)


def _banned_line():
    return ", ".join(list(FILLER) + list(AI_SLOP))


def _pairs_block():
    width = max(len(w) for w, _ in SPECIFICITY_PAIRS)
    return "\n".join('    "%s"%s  ->  "%s"' % (w, " " * (width - len(w)), s)
                     for w, s in SPECIFICITY_PAIRS)


STYLE_RULES = (
    "Style rules. The truth rules above govern WHAT you may say; these govern HOW you write it, "
    "and they matter just as much -- a truthful sentence in recruiter-speak still gets skimmed "
    "past.\n"
    "- BANNED WORDS AND PHRASES. Do not use any of these, anywhere, in either document:\n"
    "  " + _banned_line() + ".\n"
    "  If one of them is the only word that comes to mind, the sentence is not specific enough "
    "yet. Name the actual work instead.\n"
    "- SAY THE SPECIFIC THING, NOT THE CATEGORY. A number, a tool name or a proper noun beats "
    "an adjective every time:\n"
    + _pairs_block() + "\n"
    "- ONE VERB, ONE BULLET. Never open two bullets in the same section with the same verb.\n"
    "- VARY THE LENGTH. A long bullet after two short ones reads as emphasis. Five long ones in "
    "a row read as noise.\n"
    "- PLAIN WORDS. 'used' not 'utilized'; 'cut' not 'reduced'; 'built' not 'architected'; "
    "'ran' not 'oversaw the execution of'.\n"
    "- NO EM DASH, no en dash, no smart quotes, no zero-width characters. An ATS mangles them. "
    "Hyphens and straight quotes only.\n"
    "- WRITE IN THE CANDIDATE'S REGISTER, not a recruiter's. If their resume says 'shop floor', "
    "leave it as 'shop floor' -- do not promote it to 'manufacturing operations environment'. "
    "Adjust wording; do not replace their voice with yours.\n"
    "- ACCURACY OVER RHYTHM. Never soften a fact, round a number up, or add a qualifier to make "
    "a line scan better.\n"
)

PRESERVATION_RULES = (
    "Copy these exactly -- they are not yours to improve:\n"
    "- Every date range, character for character, including month prefixes "
    "('Jan 2020 - Present'). Never extend, shorten or reformat a date, and never reorder roles.\n"
    "- The candidate's name, email, phone, location and links, byte for byte.\n"
    "- Every employer and job title as written.\n"
    "- The number of bullets in each section and their order, except where the intensity rule "
    "below explicitly allows otherwise.\n"
    "- Every skill, certification, language and award already listed. You may add to those lists "
    "only where the candidate's own material supports the addition. Never remove one.\n"
)

# Three levels, from Resume-Matcher. The value is that "tailor my resume" is not one request:
# a near-miss job wants a nudge, a strong match wants the keywords worked in, and a career
# pivot wants the emphasis genuinely rebuilt. One setting served all three badly.
INTENSITY = {
    "light": (
        "Intensity: LIGHT TOUCH. Make the smallest edits that work, and only where the "
        "candidate's existing wording already lines up with something the job asks for. Do NOT "
        "add bullets or sections. Most lines should come through untouched -- if you have "
        "rewritten more than a third of them, you have gone too far."
    ),
    "keyword": (
        "Intensity: KEYWORDS WORKED IN. You may rewrite existing bullets so the job's real "
        "terminology appears wherever it is genuinely true of the candidate. Do NOT add or "
        "remove bullets, and do not change how many there are in any section."
    ),
    "full": (
        "Intensity: FULL TAILOR. You may split an existing bullet in two, or add a bullet that "
        "draws out work already described elsewhere in the candidate's material, and you may "
        "reorder sections so the most relevant experience leads. Do NOT invent a "
        "responsibility, project, employer or metric that is not already there."
    ),
}
DEFAULT_INTENSITY = "keyword"

_INTENSITY_LABELS = (
    ("light", "Light touch", "Only where your wording already matches. Nothing added."),
    ("keyword", "Work the keywords in", "Rewrites bullets to use their terms. Same bullets."),
    ("full", "Full tailor", "Can split bullets and reorder sections. Invents nothing."),
)


def intensity_choices():
    """(value, label, hint) for the tailor form. One definition, so the radio and the prompt can
    never offer different levels."""
    return _INTENSITY_LABELS


def _key(name):
    """Normalise anything at all to a known intensity key. str() rather than assuming a string:
    this is fed from request.form and from callers, and an intensity that raises would take the
    whole rewrite down over a UI setting."""
    try:
        k = str(name or "").strip().lower()
    except Exception:
        k = ""
    return k if k in INTENSITY else DEFAULT_INTENSITY


def intensity_rules(name):
    return INTENSITY[_key(name)]


def writing_rules(intensity=None):
    """The full style block to paste into a prompt."""
    return STYLE_RULES + "\n" + PRESERVATION_RULES + "\n" + intensity_rules(intensity) + "\n"


# ---------------------------------------------------------------------------------------------
# Detection, for the advisory check and the prompt evals. Whole-word/phrase only: substring
# matching turns "robust" into a hit inside "robustness" and, worse, flags real product names.
# ---------------------------------------------------------------------------------------------
_SLOP_RE = re.compile(
    r"(?<![\w-])(" + "|".join(re.escape(p) for p in sorted(AI_SLOP, key=len, reverse=True))
    + r")(?![\w-])", re.I)


def find_slop(text):
    """[(phrase_lowercased, count)] for every AI_SLOP phrase present, commonest first."""
    hits = {}
    for m in _SLOP_RE.finditer(text or ""):
        k = m.group(1).lower()
        hits[k] = hits.get(k, 0) + 1
    return sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))


def find_ats_hostile(text):
    """[(char, count)] for characters an ATS mangles. Cheap, and the one language check that is
    objectively right rather than a matter of taste."""
    t = text or ""
    return [(c, t.count(c)) for c in ATS_HOSTILE if c in t]


def repeated_openers(bullets):
    """[(verb, count)] for first words used by more than one bullet. `bullets` is a list of
    strings. Used by the evals -- the scorer has its own, section-scoped, version."""
    seen = {}
    for b in bullets or []:
        w = (b or "").strip().split(" ")[0].strip(".,:;-").lower()
        if len(w) > 2:
            seen[w] = seen.get(w, 0) + 1
    return sorted([(w, n) for w, n in seen.items() if n > 1], key=lambda kv: (-kv[1], kv[0]))


def intensity_label(name):
    """Human label for a stored/submitted intensity value, for the draft page's header. Falls
    back to the default's label rather than to an empty string -- a draft with no visible
    setting is a draft the user cannot reason about."""
    key = _key(name)
    for value, label, _hint in _INTENSITY_LABELS:
        if value == key:
            return label
    return DEFAULT_INTENSITY
