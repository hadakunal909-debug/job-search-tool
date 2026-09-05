#!/usr/bin/env python3
"""Render a stored job description as HTML, server-side.

A port of the renderer that lived in static/app.js as jdHTML(), plus two things the modal had no
room for: section labelling and inline keyword highlighting. The JS original and its TypeScript
twin (web/src/feed/jd.ts) were retired when the modal was replaced by /job; the bytes they
produced are frozen in scripts/fixtures/jd_html_baseline.json and this module reproduces them
exactly when sections and highlighting are off. scripts/test_jdrender.py is the gate.

WHY THIS IS NOT web.py OR core.py. The scraper, the digest and score_jobs all import core, and
none of them renders anything; presentation does not belong in their import cost. web.py would
mean a test that boots Flask and reads Supabase config to check a regex. This module imports the
standard library and nothing else.

THE ONE STRUCTURAL DECISION. Pass 1 produces typed NODES, not markup:

    [("h", "Requirements"), ("p", "We are..."), ("ul", ["item", "item"])]

Everything downstream works on plain text. The highlighter returns runs of plain text and the
renderer escapes each run on its way out, so escaping is the last thing that happens and the
highlighter can never see, match inside, or produce a tag. A string-in/string-out port would have
forced it to regex over HTML it had just built, and a term like "amp" or "lt" would have matched
inside an &amp; entity.

    from jdrender import render_jd
    html = render_jd(jd_text, have=["python", "sql"], missing=["kubernetes"])

FIDELITY NOTES, all four of which were live traps porting from JS:

  * JS's /i flag makes [A-Z] match lowercase as well, so JD_SECTION's "(?=[A-Z])" lookahead is
    really "(?=[a-zA-Z])". Python's re.I does the same thing to [A-Z], so the port is literal.
    It looks like a bug in both languages and must stay, because every threshold below was
    tuned against this behaviour on the live corpus.
  * Python's "$" also matches before a trailing newline; JS's (without /m) does not. \\Z here.
  * esc() is hand-written and does NOT escape quotes. The original was textContent -> innerHTML,
    which escapes &, < and > and turns NBSP into &nbsp;, and nothing else. html.escape() would
    escape quotes and break the frozen bytes.
  * JD_CUT is U+0001 and JD_ITEM_RE keeps the character before the cut instead of using a
    lookbehind, which the ES5 original could not rely on.
"""
import re

# ---------------------------------------------------------------------------------------------
# Pass 0: escaping. The only door out.
# ---------------------------------------------------------------------------------------------


def esc(s):
    """&, < and > and NBSP. Not quotes: see the module docstring."""
    s = "" if s is None else str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\u00a0", "&nbsp;"))


# ---------------------------------------------------------------------------------------------
# Pass 1: text -> typed nodes. A literal port; every constant and threshold is the original's.
# ---------------------------------------------------------------------------------------------
JD_BULLET = re.compile(r"^\s*(?:[\u2022\u00b7\u25aa\u25cf\u25e6\u2023\u2043*\u2013\u2014-]"
                       r"|\(?\d{1,2}[.)])\s+")
# INVISIBLE CHARACTERS THAT ARE NOT WHITESPACE, so nothing else in this file can see them.
# U+200B is not \s and str.strip() does NOT remove it -- ' \u200b x '.strip() is '\u200b x'.
# Workday writes one after every field colon ("Job Title : <zwsp> Program Manager"), and left in
# it defeats rstrip(":") in the stacked-field branch, re.sub(r":\Z") in add_heading,
# MD_BOLD_LINE's (?<=\S) and every (\S.*) capture here. Measured on ~2% of stored rows.
ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
# A BULLET GLYPH ALONE ON A LINE, which is nothing anybody typed: it is core._soup_text's <li>
# marker orphaned from its words by a block tag INSIDE the <li> -- <li><p>text</p></li>, the
# commonest ATS list shape. JD_BULLET cannot match it (that requires \s+ AFTER the glyph), so it
# fell through to the paragraph path and rendered as a <p> holding one bullet with its sentence
# in the <p> below. core.py no longer writes this shape, but ~4% of stored rows predate the fix
# and are not being re-scraped, so the repair has to be on READ.
#
# DELIBERATELY NARROWER THAN JD_BULLET'S CLASS. "*", "-", en dash and em dash alone on a line are
# ambiguous in the markdown and plain-text feeds -- a footnote marker, a short rule, a dash
# separator -- and _soup_text can never have produced one: it emits U+2022 and nothing else.
JD_ORPHAN_BULLET = re.compile(r"^[\u2022\u00b7\u25aa\u25cf\u25e6\u2023\u2043]\s*$")
JD_HEAD = re.compile(
    r"^(?:about|responsibilit|qualificat|requirement|what you|who you|the role|your role|"
    r"benefit|perks|compensation|skills|experience|education|duties|essential|preferred|"
    r"minimum|basic|nice to have|equal (?:employment )?opportunity|eeo|how to apply|why join|"
    r"our team|job (?:summary|description|details))", re.I)


def is_shout(t):
    """Entirely upper case, with enough letters to mean it. This was is_jd_heading's own test,
    lifted out and named because jd_nodes' hard-wrap join needs the same question answered. The
    >= 3 letter floor is load-bearing: t == t.upper() is true of any line with no cased letters
    at all, so without it "5+ / 10+ / 15+" counts as shouting."""
    return len(re.sub(r"[^A-Za-z]", "", t)) >= 3 and t == t.upper()


def is_jd_heading(t):
    if not t or len(t) > 70 or JD_BULLET.match(t):
        return False
    if is_shout(t):
        return True                                   # short ALL-CAPS line
    if t.endswith(":") and len(t.split()) <= 8:
        return True                                   # "Requirements:"
    # A known section name with no colon. Digits and $ disqualify, or "Salary: $120,000" would
    # be promoted from a fact to a heading.
    return bool(JD_HEAD.match(t)) and len(t.split()) <= 8 \
        and not re.search(r"[.;,]\Z", t) and not re.search(r"[\d$]", t)


# Two tiers, because precision matters more than recall: a false heading mid-sentence is far
# uglier than a missed one. STRONG names are unambiguous enough to match on a colon OR a
# following capital ("Overview This is a hybrid role" is how iCIMS writes it). WEAK ones are
# ordinary words that appear constantly in prose ("5 years of experience"), so they need a colon.
JD_SECTION_STRONG = (
    "job description|position purpose|position summary|role summary|"
    "essential (?:functions?|duties)|basic qualifications|minimum qualifications|"
    "preferred qualifications|additional qualifications|key responsibilities|"
    "primary responsibilities|what you(?:'|\u2019)ll (?:do|bring)|what you will do|"
    "what you bring|what we(?:'|\u2019)re looking for|what we offer|who you are|"
    "about (?:us|the role|the team|the company|the job|this role)|required skills|"
    "day in the life|nice to have|how to apply|why join(?: us)?|our team|"
    "equal (?:employment )?opportunity(?: employer)?|eeo statement|"
    "pay range|salary range|compensation range|overview")
JD_SECTION_WEAK = ("summary|responsibilities|requirements|qualifications|benefits|perks|"
                   "compensation|education|experience|skills|duties")
JD_SECTION = re.compile(
    r"\b(" + JD_SECTION_STRONG + r")\b(?::\s+|\s+(?=[A-Z]))"
    r"|\b(" + JD_SECTION_WEAK + r")\b:\s+", re.I)
JD_PARA_MAX = 360                    # chars; above this a run is split on sentence boundaries

# The iCIMS/Workday "Essential Functions" idiom: a dozen requirement lines concatenated with no
# bullet, no newline and no full stop. The boundary is a capital following a lowercase word, but
# splitting on ANY capital would wreck real prose, because the most frequent capitals in that
# position across the corpus are proper nouns (Boeing, Capital, Company, Engineering, One,
# States). So: only before words that actually START a requirement, and only inside a run that
# has already failed the punctuation test.
JD_ITEM_START = (
    "Demonstrated|Demonstrates|Ability|Abilities|Proven|Proficien(?:cy|t|cies)|Familiarity|"
    "Knowledge|Understanding|Excellent|Strong|Solid|Exceptional|Experience|Expertise|"
    "Bachelor'?s?|Master'?s?|Minimum|Preferred|Required|Must|Should|Responsible|Working|"
    "Assists?|Develops?|Ensures?|Maintains?|Performs?|Provides?|Coordinates?|Participates?|"
    "Collaborates?|Implements?|Analyzes?|Prepares?|Monitors?|Reviews?|Conducts?|Evaluates?|"
    "Recommends?|Identifies|Identify|Communicates?|Translates?|Oversees?|Establishes?|"
    "Contributes?|Executes?|Delivers?|Partners?|Serves?|Troubleshoots?|Utilizes?")
JD_ITEM_RE = re.compile(r"([a-z)\]])\s+(?=(?:" + JD_ITEM_START + r")\b)")
JD_CUT = "\u0001"                    # cannot occur in a description, so the split is unambiguous
JD_ITEM_MIN = 25                     # a real requirement line is long; short means a bad cut
# A cut is wrong if the text before it ends on a word that cannot end a sentence. Measured over
# 600 real descriptions, this is the whole of the remaining false-positive class.
JD_DANGLING = re.compile(
    r"\b(?:a|an|the|and|or|of|with|to|for|in|on|at|by|from|as|plus|per|our|your|their|its|"
    r"this|that|these|those|is|are|be|been|has|have|had|will|shall|may|any|all|each|other|"
    r"including|includes|include)\Z", re.I)
JD_SENTENCE = re.compile(r"[^.!?]+(?:[.!?]+[\"'\u2019)\]]*|\Z)")


def jd_flat_list(t):
    """A bullet-less, full-stop-less requirements run split into items, or None."""
    stops = len(re.findall(r"[.!?]", t))
    if stops / (len(t) / 1000.0) >= 6:               # already prose: use the sentence splitter
        return None
    raw = JD_ITEM_RE.sub(r"\1" + JD_CUT, t).split(JD_CUT)
    # Heal bad cuts by gluing a dangling piece onto the next one, rather than throwing the whole
    # split away: one wrong boundary should not cost the other eleven.
    parts, i = [], 0
    while i < len(raw):
        piece = raw[i].strip()
        if not piece:
            i += 1
            continue
        while JD_DANGLING.search(piece) and i + 1 < len(raw):
            i += 1
            piece += " " + raw[i].strip()
        parts.append(piece)
        i += 1
    if len(parts) < 4:                               # a lead-in plus at least 3 items
        return None
    # From 1, not 0: parts[0] is whatever preceded the first item, and it is routinely a stub.
    for j in range(1, len(parts)):
        if len(parts[j]) < JD_ITEM_MIN:
            return None                              # cut mid-sentence, abandon it
    return parts


def jd_chunk(t):
    """One run of prose -> nodes. Never a paragraph much longer than JD_PARA_MAX unless a single
    sentence is."""
    t = (t or "").strip()
    if not t:
        return []
    # Some boards flatten a real <ul> into "* a * b * c" on one line. Two or more bullets is a
    # list; a single one is a stray glyph inside a sentence.
    if len(re.findall("\u2022", t)) >= 2:
        parts = re.split(r"\s*\u2022\s*", t)
        out = []
        if parts and parts[0].strip() and t[0] != "\u2022":
            out.append(("p", parts.pop(0).strip()))
        items = [p.strip() for p in parts if p.strip()]
        if items:
            out.append(("ul", items))
        return out
    if len(t) <= JD_PARA_MAX:
        return [("p", t)]
    flat = jd_flat_list(t)
    if flat:
        return [("p", flat[0].strip()), ("ul", [f.strip() for f in flat[1:]])]
    sent = JD_SENTENCE.findall(t) or [t]
    out, buf = [], ""
    for s in sent:
        s = s.strip()
        if not s:
            continue
        if buf and len(buf) + len(s) > JD_PARA_MAX:
            out.append(("p", buf))
            buf = ""
        buf = (buf + " " + s) if buf else s
    if buf:
        out.append(("p", buf))
    return out


def jd_paragraphs(text):
    t = str(text or "").strip()
    if not t:
        return []
    out, last = [], 0
    for m in JD_SECTION.finditer(t):
        # Not a heading if it is finishing the sentence in front of it: "Cintas Corporation is
        # proud to be an" / "Equal Opportunity Employer" is one sentence, and lifting the tail
        # out of it leaves a paragraph dangling on "an". `last` is deliberately NOT advanced,
        # so the skipped run stays attached to whatever comes next.
        if JD_DANGLING.search(t[last:m.start()].strip()):
            continue
        out.extend(jd_chunk(t[last:m.start()]))
        out.append(("h", m.group(1) or m.group(2)))
        last = m.end()
    out.extend(jd_chunk(t[last:]))
    return out


# ---------------------------------------------------------------------------------------------
# Markdown artefacts. Some boards store the description as MARKDOWN and we were rendering the
# source: "**Job description**" over a row of dashes, stray "**" on their own line, and rules of
# ----- used as separators all reached the page verbatim. Measured over the 18,087 cached
# descriptions: 362 (2.0%) contain **bold**, 234 a trailing **, 44 a rule of dashes, 45 an ATX
# heading. Small, but it is the ugliest thing on the page when it happens.
#
# These are handled as MARKUP, not deleted as noise: "**Location**" followed by "-----------" is a
# setext heading, so it becomes a real heading node and joins the jump strip, rather than being
# stripped to a bare line of text. Only the punctuation ever disappears, so the no-text-is-dropped
# check (which projects to alphanumerics) still holds by construction.
MD_RULE = re.compile(r"^\s*([-=_*])\1{2,}\s*$")          # ----- / ===== / _____ / *****
MD_ATX = re.compile(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$")      # ## Heading
MD_BOLD_LINE = re.compile(r"^\s*(?:\*\*|__)(?=\S)(.+?)(?<=\S)(?:\*\*|__)\s*:?\s*$")
MD_BOLD = re.compile(r"(?:\*\*|__)(?=\S)(.+?)(?<=\S)(?:\*\*|__)", re.S)
MD_STRAY = re.compile(r"\*\*|__(?=\s|\Z)")
# Markdown backslash escapes: a writer who types "cross-functional" into a Markdown field gets
# "cross\-functional" stored, and we were printing the backslash. Restricted to PUNCTUATION on
# purpose -- a backslash before a letter or digit is not an escape, it is a Windows path or a
# regex (\S, \D, \b appear in 24 of the cached descriptions) and must survive untouched.
MD_ESCAPE = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]^_`{|}~\\])")
# A setext underline only promotes a SHORT line. A rule under a 300-character paragraph is a
# separator, not a title for it, and turning that paragraph into a heading would be worse than
# leaving the dashes in.
MD_SETEXT_MAX = 90

# ---------------------------------------------------------------------------------------------
# The metadata header some boards stack one line at a time:
#
#     Clearance Level / None / Category / Software Engineering / Location / Remote...
#
# each separated by a blank line, so every one became its own paragraph and the description
# opened with a ladder of orphaned words.
#
# WHY A VOCABULARY AND NOT A RULE. The obvious approach is "a short Title Case line followed by
# another short line is a label", and it is wrong on this corpus. Counting what actually lands in
# the label slot turns up "None", "Angular", "Software Engineering" and "Java (Programming
# Language)" -- the VALUES are shaped exactly like the labels, so a structural guess inverts pairs
# and renders worse than the stacking it replaces. Matching known ATS field names cannot make that
# mistake: anything not on this list is left exactly as it renders today.
#
# Measured: only 12 of 18,087 cached descriptions (0.1%) open with this shape, so the list is
# deliberately small. Add to it when a board turns up that needs it.
FIELD_LABELS = frozenset([
    "clearance level", "security clearance", "clearance", "public trust", "citizenship",
    "category", "job category", "department", "business unit", "job family", "discipline",
    "location", "work location", "job location", "remote type", "telecommuting options",
    "travel required", "relocation",
    "job type", "employment type", "employment status", "position type", "hire type",
    "contract type", "work schedule", "schedule", "shift", "seniority level", "career level",
    "requisition type", "requisition id", "req id", "req#", "job id", "posting id",
    "posted date", "date posted", "close date",
    "salary", "salary range", "pay range", "compensation", "benefits eligible", "flsa status",
    "key skills for success", "key skills", "skills",
    "education", "experience", "years of experience",
    # WORKDAY'S OWN HEADER BLOCK, which writes the label with a space BEFORE the colon.
    # "job description" is deliberately NOT here: it is a very common opening HEADING, it is in
    # JD_SECTION_STRONG and in the summary bucket, and making it a field would steal it from the
    # heading path and from the jump strip. It needs no fixing either -- with the zero-width
    # strip above, "Job Description :" reaches is_jd_heading via JD_HEAD and add_heading's
    # trailing-colon trim renders it as the heading it is.
    "company", "job title", "job posting location", "posting location",
    # "worker type" / "Regular" is Workday's, and without it the pair rendered as the Overview's
    # own subtitle with the word "Regular" as that section's first line.
    # THIS LIST IS IN PASS 1, so an addition can move the frozen baseline. "time type" does occur
    # in jd_samples[18] and the bytes did not move, because _field_label only fires on a line
    # that IS the label -- an inline mention is untouched. Re-run scripts/test_jdrender.py after
    # adding to this list; do not reason about whether the words appear.
    "worker type", "time type", "job level", "job function",
])


# THE SAME FIELDS, ON ONE LINE. Workday writes its header as "Company : AHI agilon health, inc."
# -- label, space, colon, value -- so the STACKED parser below never saw it and each line opened
# the description as its own stray paragraph.
#
# THE VALUE IS BOUNDED, AND THAT BOUND IS THE WHOLE RULE. jd_samples.json[16] is a real posting
# flattened onto ONE line that starts "Job Title: Senior Program and Project Manager Location:
# Remote Duration: 6 Months ..." -- an unbounded (\S.*) capture turns that entire description
# into a single <dd>. A value running past 69 characters, or holding a second colon, is prose
# wearing a label; that is the same 70-character judgement the stacked branch already makes
# about its own candidate values.
FIELD_INLINE = re.compile(r"^\s*([^:]{2,40}?)\s*:\s*(\S[^:]{0,68})$")


def _field_label(t):
    """The canonical field name for a line, or "" if it is not one. Tolerates a trailing colon."""
    k = re.sub(r"\s+", " ", (t or "").strip().rstrip(":").strip()).lower()
    return k if k in FIELD_LABELS else ""


def strip_md(t):
    """Drop emphasis markers and backslash escapes, keeping the words. Runs on every line that
    survives as text."""
    t = MD_BOLD.sub(r"\1", t)
    t = MD_STRAY.sub("", t)
    t = MD_ESCAPE.sub(r"\1", t)
    return t.strip()


def jd_nodes(text):
    """The whole of pass 1: raw description -> typed nodes."""
    src = str("" if text is None else text).replace("\r\n", "\n").replace("\r", "\n") \
        .replace("\u00a0", " ")
    # Gated on a hit so the 98% of descriptions holding none are byte-identical by
    # construction. The re-collapse is [^\S\n] and not \s because removing a zero-width leaves
    # "Location :  Grand Rapids" with a doubled space, and collapsing \n along with it would
    # destroy the line structure this entire pass is built on.
    if ZERO_WIDTH.search(src):
        src = re.sub(r"[^\S\n]{2,}", " ", ZERO_WIDTH.sub("", src))
    out, para, lst = [], [], None

    def flush_para():
        if para:
            out.extend(jd_paragraphs(" ".join(para)))
            del para[:]

    def flush_list():
        nonlocal lst
        if lst:
            out.append(("ul", lst))
        lst = None

    def _before_body():
        """Has the description itself started yet? `para` and `lst` count, and that is the fix:
        prose sits in the buffer until something flushes it, so reading `out` alone let a field
        name halfway down a description with no blank line above it be pulled out of context
        anyway -- exactly what the comment on the stacked branch promises cannot happen. The
        LATE case in scripts/test_jdrender.py passes today only because its fixture has that
        blank line."""
        return not para and not lst and not any(k in ("p", "ul") for k, _v in out)

    def add_heading(txt):
        # Some boards mark a FIELD up as a heading: "##### **REQ#:****RQ225292**" arrives here as
        # "REQ#:RQ225292". While the metadata block is still open, put those in the field list
        # instead of leaving three one-line headings stranded above the description. Gated on the
        # same vocabulary, so an ordinary "Why Join Us: The Team" heading is untouched.
        m2 = re.match(r"^\s*([^:]{2,40}?)\s*:\s*(\S.*)$", txt or "")
        if m2 and _field_label(m2.group(1)) and _before_body():
            flush_para()
            flush_list()
            out.append(("kv", (m2.group(1).strip(), [m2.group(2).strip()])))
            return
        flush_para()
        flush_list()
        out.append(("h", re.sub(r":\Z", "", txt).strip()))

    lines = src.split("\n")
    # A SHOUTED LINE IS NEVER A CONTINUATION -- unless the whole posting shouts. Some ATS feeds
    # are upper case end to end, and there this rule would split every hard-wrapped fragment
    # into its own paragraph and turn the description into a ladder. Above a quarter of the
    # lines, shouting is the house style and carries no signal. The quarter is a judgement, not
    # a measurement.
    _nonblank = [l.strip() for l in lines if l.strip()]
    notice_ok = sum(1 for l in _nonblank if is_shout(l)) <= max(1, len(_nonblank) // 4)
    i = 0
    while i < len(lines):
        t = lines[i].strip()
        i += 1
        if not t:                        # a blank ends the block; runs of them vanish
            flush_para()
            flush_list()
            continue
        # Joined ONLY to the line immediately below, and BEFORE the setext lookahead: a bare
        # glyph does not match JD_BULLET, so a bullet sitting above a "-----" rule is otherwise
        # promoted to a heading named after the glyph. _soup_text's collapse emits no blank
        # lines at all, so a blank under a lone glyph means some other writer put it there and
        # it is not ours to bridge. Anything that is not plain content below -- a rule, a
        # heading, another bullet, another orphan, the end of the text -- means the glyph has no
        # words to carry, so it is dropped: a bullet with no item is not information, and it is
        # not alphanumeric, so test_jdrender's no-text-is-dropped projection cannot see it go.
        if JD_ORPHAN_BULLET.match(t):
            nxt = lines[i].strip() if i < len(lines) else ""
            if not (nxt and not MD_RULE.match(nxt) and not MD_ATX.match(nxt)
                    and not MD_BOLD_LINE.match(nxt) and not MD_STRAY.fullmatch(nxt)
                    and not JD_BULLET.match(nxt) and not JD_ORPHAN_BULLET.match(nxt)):
                continue
            # Re-formed as the line _soup_text should have written, then dropped through to the
            # ordinary bullet branch below -- so a repaired row and a freshly scraped one cannot
            # render differently. Not gated on is_jd_heading(nxt): <li><p>RESPONSIBILITIES</p>
            # </li> is a shouted list ITEM, and promoting it would be the wrong repair.
            t = "\u2022 " + nxt
            i += 1
        # A rule on its own line. Its text (if any) was consumed by the setext branch below, so
        # anything reaching here is a bare separator: end the block and drop the punctuation.
        if MD_RULE.match(t):
            flush_para()
            flush_list()
            continue
        if MD_STRAY.fullmatch(t):        # a line that is only "**" \u2014 pure noise
            continue
        m = MD_ATX.match(t)
        if m:
            add_heading(strip_md(m.group(1)))
            continue
        # Setext: this line is titled by the rule UNDER it. Consume both.
        nxt = lines[i].strip() if i < len(lines) else ""
        if nxt and MD_RULE.match(nxt) and len(t) <= MD_SETEXT_MAX and not JD_BULLET.match(t):
            add_heading(strip_md(t))
            i += 1
            continue
        # The same fields written INLINE -- "Company : Acme" -- which the stacked parser below
        # cannot see, because _field_label only recognises a line that is JUST a label. Same
        # vocabulary and the same "only before the description starts" gate; the value bound
        # lives in FIELD_INLINE itself, and it is what stops a one-line posting being eaten
        # whole by its own opening words.
        mi = FIELD_INLINE.match(strip_md(t))
        if mi and _field_label(mi.group(1)) and _before_body():
            flush_para()
            flush_list()
            out.append(("kv", (mi.group(1).strip(), [mi.group(2).strip()])))
            continue
        # A stacked metadata field, but ONLY while the description has not started yet: the moment
        # real prose or a list appears we stop looking, so a "Location" mentioned halfway down a
        # paragraph-heavy description can never be pulled out of its context.
        if _field_label(strip_md(t)) and _before_body():
            label = strip_md(t).rstrip(":").strip()
            vals, j = [], i
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt:                          # blank lines separate the stacked lines
                    j += 1
                    continue
                if (len(nxt) > 70 or JD_BULLET.match(nxt) or MD_RULE.match(nxt)
                        or MD_ATX.match(nxt) or MD_BOLD_LINE.match(nxt)
                        or _field_label(strip_md(nxt)) or re.search(r"[.!?]\Z", nxt)):
                    break                            # the next field, a heading, or real prose
                vals.append(strip_md(nxt))
                j += 1
                if len(vals) >= 8:                   # a runaway list is not a metadata field
                    break
            if vals:
                flush_para()
                flush_list()
                out.append(("kv", (label, vals)))
                i = j
                continue
            # No value under it: fall through and let it be treated as ordinary text.
        m = MD_BOLD_LINE.match(t)        # "**Location**" alone is a heading even with no rule
        if m:
            add_heading(strip_md(m.group(1)))
            continue
        bm = JD_BULLET.match(t)
        if bm:
            flush_para()
            # A marker carrying nothing but emphasis punctuation ("* **", "- __") used to
            # append "" and render as an empty <li>. Skip the ITEM, not the line: the paragraph
            # above still ends here, and a list already open stays open, so one junk marker in
            # the middle of a real list does not split it in two. flush_list needs no matching
            # guard -- lst is only ever assigned None, or a list appended to on the same pass,
            # so an all-empty list is unreachable and a check there would be dead code reading
            # like a live invariant.
            item = strip_md(t[bm.end():].strip())
            if not item:
                continue
            if lst is None:
                lst = []
            lst.append(item)
            continue
        t = strip_md(t)
        if not t:                        # was nothing but emphasis punctuation
            continue
        if is_jd_heading(t):
            add_heading(t)
            continue
        flush_list()
        # Hard-wrapped prose: a long previous line that doesn't end a sentence is mid-paragraph,
        # so join onto it. Otherwise start a new <p>, so separate one-line statements do not get
        # glued into a wall.
        # "ONLY CANDIDATES LOCATED IN THE TRAVERSE CITY, MI AREA WILL BE CONSIDERED - THIS IS A
        # HYBRID OPPORTUNITY!" is 104 characters, so is_jd_heading's <= 70 gate rightly declines
        # to call it a heading -- and this join then glued it onto the end of the paragraph
        # above, mid-sentence. It is a standalone statement either way, so it gets its own <p>.
        # This cannot promote ordinary prose: is_jd_heading has already claimed every caps line
        # of 70 characters or fewer before control reaches here, so is_shout can only ever fire
        # on a LONG one. A hard-wrapped acronym run ("AWS GCP AZURE") never gets this far.
        prev = para[-1] if para else ""
        if prev and ((notice_ok and (is_shout(t) or is_shout(prev)))
                     or not (len(prev) > 62 and not re.search(r"[.:;!?]\Z", prev))):
            flush_para()
        para.append(t)
    flush_para()
    flush_list()
    return out


# ---------------------------------------------------------------------------------------------
# Pass 2: sort the description into OUR sections, in OUR order.
# ---------------------------------------------------------------------------------------------
# THIS PASS REORDERS, AND IT USED TO REFUSE TO. The rule here read "NOTHING IS REORDERED", on the
# grounds that putting Requirements above a role summary misrepresents what the employer wrote.
# That was right about PROSE and wrong about the product. Somebody working a job search opens
# twenty postings a day; twenty running orders means hunting for the qualifications section
# twenty times, and the employer's sequence is not a thing any of them chose -- it is whatever
# their ATS template emitted. Reversed 2026-09-05 at the owner's direction: the presentation is
# ours and it is fixed, the way LinkedIn's is.
#
# THREE PARTS OF THE OLD RULE SURVIVE, AND THEY ARE THE PARTS THAT WERE LOAD-BEARING:
#   * THE EMPLOYER'S OWN HEADING TEXT IS KEPT and rendered under ours. "Basic Qualifications"
#     and "Preferred Qualifications" both land in a qualifications bucket and mean opposite
#     things; overwriting either is information loss. Ours names the section, theirs labels the
#     run. scripts/test_job_page.py asserts both strings survive.
#   * NOTHING IS DROPPED AND NOTHING IS REWORDED. Every node lands in exactly one bucket, and
#     scripts/test_jdrender.py projects the whole render back to alphanumerics to prove it.
#   * ORDER WITHIN A BUCKET IS STILL THE EMPLOYER'S. The regroup is stable, so their sequence
#     survives inside each section; only the sections themselves move.
#
# EIGHT BUCKETS. `skills` and `about` are splits; `other` is the catch-all that makes the stack
# TOTAL, which a fixed order needs and the six-bucket version did not have: a heading this module
# did not recognise used to carry no bucket at all, and under a fixed order its content would
# have had nowhere to go.
#
# ORDER IS THE ALGORITHM: classify_heading returns the FIRST match, so a bucket that is a special
# case of another has to be listed above it.
#   * `about` ABOVE `summary`, or "About Us" reads as a role summary. The split is the employer
#     versus the job, and it earns its keep: a third of postings open with three paragraphs of
#     company blurb, and web.py renders this bucket down in the About <Company> section instead
#     of in front of the job.
#   * `pref` above `req`, unchanged: "Preferred Qualifications" contains both words.
#   * `req` ABOVE `skills`, so "Required Skills" stays a requirement and a bare "Skills" heading
#     falls through. THIS IS WHY A BARE `skills` CAME OUT OF `req`'S REGEX -- while they shared a
#     bucket there was no way to tell "Required Skills" from "Skills", and the owner asked for
#     knowledge-and-skills as a section of its own.
# ORDER IS THE ALGORITHM AND IT IS NOT THE DISPLAY ORDER. classify_heading returns the FIRST
# match, so this list is sorted by SPECIFICITY, not by SEC_ORDER. Four entries sit above the
# bucket you would expect, and each is a heading that was landing in the wrong section:
#   * `other` ABOVE `req`, or "Physical Requirements", "Travel Requirements" and "Clearance
#     Requirements" all match `requirement` and land in Required Qualifications. Those three
#     alternations were unreachable where they were, and 44 headings in an 8,000 sample were
#     going to the wrong bucket because of it.
#   * `ben` ABOVE `summary`, or "Benefits Overview" matches the bare `overview` and is filed as
#     a role summary; and above `req`, or "Basic Compensation" and "Minimum Salary" match
#     `basic` / `minimum`.
#   * the generic `about` BELOW `summary`, so "About the Role" and "About This Opportunity" are
#     claimed as role summaries and only what is left is about the employer.
_SEC = [
    ("about", re.compile(
        r"about us|about (?:the )?company|about our (?:company|organi[sz]ation)|"
        r"who we are|our (?:company|story|mission|values|culture|purpose)|"
        r"company overview|why (?:work )?(?:with|at) us", re.I)),
    # "About Our Team" used to land here while "About the Team" landed in summary, which is two
    # spellings of one heading in two different sections. `team` came out; both are a summary now.
    ("other", re.compile(
        r"how to apply|application process|physical (?:demands|requirements|abilities)|"
        r"work environment|working conditions|travel requirement|clearance requirement|"
        r"security clearance|additional information|other information|"
        r"licen[sc]e[s]? and certificat", re.I)),
    ("ben", re.compile(
        r"benefit|perk|compensation|salary|(?:pay|salary|compensation) range|what we offer|"
        r"why join|total rewards", re.I)),
    ("summary", re.compile(
        r"about (?:the|this|our) (?:role|job|position|opportunity|team)|"
        r"job (?:summary|description|details|overview)|position (?:purpose|summary|overview)|"
        r"role (?:summary|overview)|overview|(?:the|your) (?:role|opportunity|impact)|"
        r"the position", re.I)),
    # "About Lyra Health" -- the company blurb heading that nobody writes as "About Us", and the
    # commonest form of it. BELOW summary on purpose, so the "About the ..." role headings are
    # claimed first. Safe to leave this broad because classify_heading only ever sees a line
    # is_jd_heading already accepted, which caps it at 70 characters and eight words -- a
    # sentence opening "About the work you will be doing here every day" never reaches it.
    # NOT written as `about [A-Z]\S+`: re.I makes [A-Z] match lowercase too (the JS-parity quirk
    # documented at the top of this file), so the capital would buy nothing.
    #
    # "ABOUT YOU" IS EXCLUDED and that exclusion is load-bearing. It is a QUALIFICATIONS heading,
    # and `about` is the one bucket render_split moves off the description -- so without this it
    # does not merely mislabel the requirements, it relocates them to the foot of the page.
    ("about", re.compile(r"^about\s+(?!you\b)\S", re.I)),
    ("resp", re.compile(
        r"responsibilit|what you.?ll do|what you will do|essential (?:functions?|duties)|"
        r"duties|day in the life|key responsibilities|primary responsibilities|"
        r"what you.?ll be doing|in this role you|you will\b", re.I)),
    ("pref", re.compile(
        r"preferred|preferable|nice to have|nice-to-have|desired|desirable|"
        r"additional qualificat|bonus|a plus|good to have|pluses|we prefer", re.I)),
    ("req", re.compile(
        r"requirement|qualificat|what you.?ll (?:need|bring)|what you bring|"
        r"what we.?re looking for|who you are|about you|you have\b|we require|"
        r"required skills|experience|education|"
        r"minimum|basic|must have|must-have", re.I)),
    # Bare skill and tool headings, AFTER req so "Required Skills" never reaches here.
    ("skills", re.compile(
        r"skills|competenc|tools|technolog|tech stack|proficienc", re.I)),
]
_LEGAL_HEAD = re.compile(
    r"equal (?:employment )?opportunity|eeo|e-?verify|affirmative action|"
    r"reasonable accommodation|drug.?(?:free|screen)|background check|pay transparency|"
    r"applicant (?:privacy|rights)|fair chance|export control|itar|at.?will", re.I)
_LEGAL_BODY = re.compile(
    r"equal opportunity employer|with(?:out)? regard to race|regardless of race|"
    r"protected veteran|reasonable accommodation|drug.?free workplace|"
    r"criminal (?:history|background)|pay transparency|applicants? with disabilit", re.I)
# OUR WORDS FOR THE SECTIONS. These are chrome this renderer adds, not anything the employer
# wrote, which is why _groups_html puts them in a <span class="jdlabel"> that the no-text-lost
# projection strips -- exactly the carve-out <summary> already has for the legal disclosure.
SEC_LABELS = {"summary": "Overview", "resp": "Responsibilities",
              "req": "Required Qualifications", "pref": "Preferred Qualifications",
              "skills": "Skills & Tools", "ben": "Compensation & Benefits",
              "about": "About the Company", "other": "Additional Information"}
# THE CANONICAL STACK. Every posting renders in this order, and a bucket with nothing in it does
# not render at all and says nothing about why -- the owner's instruction, 2026-09-05: "if there
# is something they should be over there. If not, it is fine."
#
# `about` sits late because the reader came for the job, not the employer, and `other` is last
# before the legal disclosure because a heading we could not place is the least likely thing on
# the page to be worth reading first.
SEC_ORDER = ("summary", "resp", "req", "pref", "skills", "ben", "about", "other")


def classify_heading(t):
    """A SEC_ORDER key, "legal", or "" for one heading's own words.

    "" means "not a section name this module knows", which is NOT the same as "other" -- see
    _group, which makes an unrecognised heading inherit rather than inventing a section for it.
    """
    if _LEGAL_HEAD.search(t or ""):
        return "legal"
    for key, rx in _SEC:
        if rx.search(t or ""):
            return key
    return ""


# ---------------------------------------------------------------------------------------------
# Pass 2b: infer a section for text the employer never labelled.
# ---------------------------------------------------------------------------------------------
# WHY THIS HAS TO EXIST. Measured 2026-09-04 over 209 active rows: 74% of stored descriptions
# contain no newline at all and 44% render with no bullet list. Over the 20 real descriptions in
# scripts/fixtures/jd_samples.json, heading classification alone finds a section in 12 and finds
# RESPONSIBILITIES -- the second row of the stack -- in exactly one. A heading-only engine leaves
# most postings as a single Overview block, which is the wall of text this whole pass exists to
# break up.
#
# THE UNIT IS A WHOLE NODE, NEVER A SENTENCE, and that is the guardrail. jd_flat_list has already
# cut an unbroken run into items on JD_ITEM_RE; this classifies what that produced. Re-splitting
# prose on a guess about where a requirement starts is the failure mode that kept this deferred,
# and nothing here does it.
#
# THE TWO CLASSES WERE ALREADY SITTING IN JD_ITEM_START and nothing read them apart: half those
# words open a requirement ("Proven", "Bachelor's", "Minimum") and half open a duty ("Ensures",
# "Coordinates", "Oversees"). JD_ITEM_START ITSELF IS NOT TOUCHED -- it feeds JD_ITEM_RE, which
# decides where pass 1 cuts, and pass 1 is byte-frozen. The imperative forms below ("Lead",
# "Build", "Own") are new here and deliberately absent there.
JD_REQ_START = (
    "Demonstrated|Demonstrates|Ability|Abilities|Proven|Proficien(?:cy|t|cies)|Familiarity|"
    "Knowledge|Understanding|Excellent|Strong|Solid|Exceptional|Experience|Expertise|"
    "Bachelor'?s?|Master'?s?|Minimum|Required|Must|Should|Advanced|Fluent|Comfortable|"
    # `Working knowledge` / `Working experience` are requirements, and the bare `Working` that
    # used to catch them was removed from JD_RESP_START for reading "Working knowledge of
    # Kubernetes" as a duty. Naming the two real phrases here is what keeps the requirement
    # reading without bringing the false positive back.
    "Working (?:knowledge|experience|familiarity)|"
    "Track record|Hands.on|Deep|Prior|At least")
# SEVEN WORDS CAME OUT OF THIS LIST because each is a noun as often as a verb, and the noun
# reading is usually a REQUIREMENT or a benefit rather than a duty:
#   "Working knowledge of Kubernetes"        was read as a responsibility  (Working)
#   "Work authorization: must be authorized" was read as a responsibility  (Work)
#   "Plan documents are available from HR"   was read as a responsibility  (Plan)
#   "Run rate of two million events"         was read as a responsibility  (Run)
# `Work` survives only in its unambiguous phrasal forms. `Working` is deliberately still in
# JD_ITEM_START, which is a different question -- that list answers "where does a line start",
# and "Working knowledge of X" does start one.
# `Drive` was listed twice; the second was dead.
JD_RESP_START = (
    "Responsible|Assists?|Develops?|Ensures?|Maintains?|Performs?|Provides?|"
    "Coordinates?|Participates?|Collaborates?|Implements?|Analyzes?|Prepares?|Monitors?|"
    "Reviews?|Conducts?|Evaluates?|Recommends?|Identifies|Identify|Communicates?|Translates?|"
    "Oversees?|Establishes?|Contributes?|Executes?|Delivers?|Partners?|Serves?|Troubleshoots?|"
    "Utilizes?|Lead|Leads|Build|Builds|Design|Designs|Own|Owns|Drive|Drives|Manage|Manages|"
    "Define|Defines|Support|Supports|Create|Creates|Champion|Deliver|Act as|"
    "Work (?:with|closely|across|alongside|in partnership)|"
    "Craft|Ship|Engage|Facilitate|Organi[sz]e")
# PREFERRED IS TESTED FIRST because a line carries both signals constantly: "5+ years of Python
# preferred" is a preference, not a floor, and reading it as a floor is the one mislabel that
# could stop somebody applying for a job they can do.
_INF_PREF = re.compile(
    r"\bpreferred\b|\bpreferably\b|\ba plus\b|\bnice to have\b|\bbonus points\b|"
    r"\bdesirable\b|\bideally\b|\bwould be a plus\b", re.I)
_INF_REQ_OPEN = re.compile(r"^(?:" + JD_REQ_START + r")\b", re.I)
_INF_REQ_IN = re.compile(
    r"\b\d+\+?\s*(?:or more\s*)?years?\b|\bbachelor|\bmaster'?s\b|\bdegree\b|"
    r"\bph\.?d\b|\bb\.?s\.?\b|\bm\.?s\.?\b", re.I)
_INF_RESP_OPEN = re.compile(r"^(?:" + JD_RESP_START + r")\b", re.I)
# EVERY ALTERNATIVE HERE NAMES A BENEFIT, and two that read as though they did are gone:
#   * `health care` matched "a leading provider of evidence-based mental health care" three
#     times in jd_samples[10] and filed Lyra Health's company blurb under Compensation.
#   * a bare `equity` matches "diversity, equity and inclusion", which is not a pay package and
#     appears in a large share of the corpus -- it would have mislabelled a DEI paragraph as
#     compensation on thousands of postings.
# A bare `compensation` went the same way ("the compensation and benefits team" is a department).
# The asymmetry that justifies the strictness: a missed benefits section costs a heading, a
# wrong one puts our label on the employer's words and is the failure this tier is not allowed.
_INF_BEN = re.compile(
    r"\bsalary\b|\b(?:pay|salary|compensation) range\b|\btotal compensation\b|"
    r"\bcompensation package\b|\b401\(?k\)?\b|\bpaid time off\b|\bPTO\b|"
    r"\bhealth insurance\b|\bhealth benefits\b|\bmedical,? (?:and )?dental\b|"
    r"\bdental,? (?:and )?vision\b|\bstock options?\b|\bRSUs?\b|\bbase pay\b|"
    r"\bequity (?:grant|award|package|compensation)\b|\bhourly rate\b|"
    r"\btuition reimbursement\b|\bparental leave\b|\bpaid holidays\b|"
    r"\bcomprehensive benefits?\b|\bbenefits package\b", re.I)
_INF_ABOUT = re.compile(
    r"\bour (?:mission|vision|values|story|culture)\b|\bwe are a\b|\bfounded in\b|"
    r"\bis a (?:leading|global|fast.growing|publicly traded)\b", re.I)

INFER_SHARE = 0.6           # of a list's items, before that list is called a section
INFER_MIN_CHARS = 80        # a paragraph shorter than this is a stub, not a section


def _infer_one(t):
    """The bucket one line's own shape argues for, or "" when nothing does."""
    t = (t or "").strip()
    if not t:
        return ""
    if _INF_PREF.search(t):
        return "pref"
    if _INF_REQ_OPEN.match(t) or _INF_REQ_IN.search(t):
        return "req"
    if _INF_RESP_OPEN.match(t):
        return "resp"
    if _INF_BEN.search(t):
        return "ben"
    if _INF_ABOUT.search(t):
        return "about"
    return ""


def infer_bucket(node):
    """The bucket a whole node argues for, or "" to leave it where it is.

    A LIST IS DECIDED BY MAJORITY VOTE OF ITS ITEMS and is never split: a list is one thought the
    employer wrote, and putting half of it under Responsibilities and half under Requirements is
    the mislabelling risk in its purest form. A tie between two buckets means the list is mixed,
    and mixed means we do not know -- so it stays where it is.

    A PARAGRAPH IS DECIDED ON HOW IT OPENS, not on anything it merely mentions. Prose about the
    team routinely contains "five years"; filing it under Required Qualifications on that alone
    would be exactly the confident wrong answer this tier is not allowed to give. The two
    exceptions are compensation and company blurb, which are recognisable anywhere in a
    paragraph and are not phrased as an opening verb.
    """
    kind, val = node
    if kind == "ul":
        items = [i for i in val if i and i.strip()]
        if len(items) < 2:
            return ""
        votes = {}
        for item in items:
            key = _infer_one(item)
            if key:
                votes[key] = votes.get(key, 0) + 1
        if not votes:
            return ""
        clear = [k for k, n in votes.items() if n / float(len(items)) >= INFER_SHARE]
        return clear[0] if len(clear) == 1 else ""
    if kind == "p":
        if len(val or "") < INFER_MIN_CHARS:
            return ""
        # THE DECISION IS MADE HERE AND NOT BY _infer_one, and that is the whole point of this
        # branch. _infer_one lets _INF_REQ_IN match a degree word or a year count ANYWHERE in the
        # text, which is right for a list item -- an item is one requirement -- and wrong for a
        # paragraph, which is what the docstring above says.
        #
        # THE BUG THIS FIXES, because it is subtle and it shipped: an Abbott benefits paragraph
        # ("Employees can qualify for free medical coverage ... Tuition reimbursement ... an
        # affordable path to getting a bachelor's degree") passed the gate on `tuition
        # reimbursement`, then _infer_one saw `bachelor` and returned `req`. The paragraph opened
        # a Required Qualifications section, and the EIGHT paragraphs after it -- the role
        # summary, the opportunity, the responsibilities -- all inherited it. The gate and the
        # classifier have to agree, so now the signal that opens the gate IS the answer.
        if _INF_PREF.search(val):
            return "pref"
        if _INF_REQ_OPEN.match(val):
            return "req"
        if _INF_RESP_OPEN.match(val):
            return "resp"
        if _INF_BEN.search(val):
            return "ben"
        if _INF_ABOUT.search(val):
            return "about"
    return ""


def _node_text(node):
    if node[0] == "ul":
        return " ".join(node[1])
    if node[0] == "kv":
        return node[1][0] + " " + " ".join(node[1][1])
    return node[1]


MAX_LEGAL_SHARE = 0.4                # of the description's characters


def split_boilerplate(nodes):
    """(body, legal) where legal is the trailing run of EEO and legal notices, possibly empty.

    Scanned BACKWARDS from the end and taken as one contiguous run. Several postings quote EEO
    language mid-body under "Our Values", and a forward scan would swallow the rest of the
    document from the first match onward.

    A LIST HAS TO BE LEGAL THROUGHOUT, not merely mention a notice. A list is ONE node, so
    matching it on any single item cost a real description most of its content: one sample is a
    single 2,944 character list whose last item ends "...is an Equal Opportunity Employer", and
    the whole requirements list went behind the disclosure. Requiring a majority of the items to
    match separates a boilerplate list, where every line is a notice, from a requirements list
    with one stray mention. The asymmetry justifies the strictness on its own: a missed notice
    stays visible and costs nothing, a swallowed requirements list is the job disappearing.

    Three aborts, in order:
      * a run covering every node, because a description that renders as one closed <details>
        looks broken
      * a run over MAX_LEGAL_SHARE of the text, the same failure by proportion rather than by
        count, which catches the list case's near misses
      * a run that is one short node, because a disclosure hiding a single sentence is worse
        than the sentence
    """
    if not nodes:
        return nodes, []
    i = len(nodes)
    while i > 0:
        kind, val = nodes[i - 1]
        if kind == "ul":
            hits = sum(1 for item in val if _LEGAL_BODY.search(item))
            if not val or hits * 2 < len(val):
                break
        else:
            txt = _node_text(nodes[i - 1])
            if not ((_LEGAL_HEAD.search(txt) if kind == "h" else None)
                    or _LEGAL_BODY.search(txt)):
                break
        i -= 1
    legal = nodes[i:]
    if not legal or i == 0:
        return nodes, []
    total = sum(len(_node_text(n)) for n in nodes) or 1
    if sum(len(_node_text(n)) for n in legal) / float(total) > MAX_LEGAL_SHARE:
        return nodes, []
    if len(legal) == 1 and len(_node_text(legal[0])) < 120:
        return nodes, []
    return nodes[:i], legal


# ---------------------------------------------------------------------------------------------
# Pass 2a: reshape nodes that are hiding structure. Cuts and joins; never moves, never rewrites.
# ---------------------------------------------------------------------------------------------
# SEPARATE FROM ASSIGNMENT ON PURPOSE, because the invariant is then checkable on its own:
# concatenate the node text before and after and the projection is identical, IN ORDER. Nothing
# downstream cuts anything, so nothing downstream can lose text.
#
# SECTIONS-ONLY. None of this may reach jd_chunk, whose output is frozen in
# scripts/fixtures/jd_html_baseline.json -- see render_jd's early return.
#
# WHY IT EARNS ITS PLACE. Measured over 3,000 stored descriptions of 600+ characters: 28% carry a
# section heading buried mid-paragraph, 14% carry a list flattened onto one line with " - "
# between the items, and 53% render with no bullet list anywhere at all. Those are the same
# wall of text from three different directions.

# A SENTENCE-INITIAL, TITLE-CASE, CANONICAL PHRASE. All three conditions carry weight:
#   * SENTENCE-INITIAL (the (?<=[.!?;:]) lookbehind) is the one that matters most. Without it
#     this cuts "translate requirements into actionable plans" and "offers benefits to full time
#     employees" in half, leaving a dangling half-sentence at the top of a section -- the ugliest
#     failure this feature can produce.
#   * NO re.I, so the phrase must be Title Case as the employer wrote it. That is how every ATS
#     template writes a heading and it is not how anybody writes prose.
#   * FOLLOWED BY A CAPITAL, so the heading has something to head.
# Deliberately narrower than JD_SECTION, which runs in pass 1 and is frozen.
# `\A` MATTERS AS MUCH AS THE SENTENCE ANCHOR. A flattened description routinely puts the marker
# at the START of a paragraph -- "The Opportunity The Associate Product Manager position works
# out of ..." is one node -- where there is no preceding punctuation to look behind at. Without
# it that whole paragraph inherits whatever bucket came before, and on the Abbott posting seven
# consecutive paragraphs of role content inherited Compensation and Benefits.
SPLIT_PHRASE = re.compile(
    r"(?:\A|(?<=[.!?;:])\s+)("
    r"What You(?:'|\u2019)?ll (?:Do|Need|Bring|Be Doing|Own|Work On)|What You Need to Have|"
    r"The Opportunity|Preferred Experience|Preferred Skills|Education and Experience|"
    r"Your Responsibilities|Core Responsibilities|What Y[Oo][Uu](?:'|\u2019)?LL DO|"
    r"What You Bring|What We(?:'|\u2019)?re Looking For|What We Offer|What You Will Do|"
    r"These Will Help You Stand Out|Work Model|Job Description|About Us|About the Role|"
    r"Basic Qualifications|Preferred Qualifications|Minimum Qualifications|"
    r"Additional Qualifications|Required Qualifications|Required Skills|"
    r"Key Responsibilities|Primary Responsibilities|Essential Duties|Essential Functions|"
    r"Responsibilities|Qualifications|Requirements|Nice to Have|Bonus Points|"
    r"Salary Range|Pay Range|Compensation Range|Who You Are|Your Impact|The Role|Our Team|"
    r"Why Join Us|Benefits|Perks|Education and Experience|Knowledge, Skills and Abilities"
    r")(?=\s+[A-Z0-9])")

# A LIST FLATTENED ONTO ONE LINE WITH HYPHENS, which jd_chunk does not recover: it handles the
# \u2022 form (line 188) and the JD_ITEM_START form (jd_flat_list), not this one. Amazon writes
# every requirement this way -- "... experience - 2+ years of ... - 1+ years of ..." -- and
# without this the whole posting is one Overview.
HYPHEN_RUN = re.compile(r"\s+[-\u2013]\s+(?=[A-Z0-9])")
HYPHEN_MIN_ITEMS = 3

# A LEGAL NOTICE WELDED ONTO THE END OF A REAL PARAGRAPH, which is the shape the disclosure
# scanner could not do anything with. Measured over 800 descriptions whose notices were NOT
# collapsed: 71% are a SINGLE node, and reading them showed why lifting the node whole is the
# wrong fix -- one is a 2,496 character benefits list ("Medical, dental & vision ... 401(k) ...
# Life Insurance ...") with an EEO sentence on the end. Hiding that node hides the benefits.
#
# So this CUTS, and lifting is a separate decision made afterwards on the piece that is only a
# notice. Sentence-initial for the same reason SPLIT_PHRASE is: a mid-sentence cut leaves a
# dangling half-sentence, and "we consider all applicants" appears inside real prose.
LEGAL_OPEN = (
    r"All qualified applicants|Qualified applicants will|"
    r"(?:We|The [A-Z][\w&.\- ]{1,40}?) (?:is|are) an [Ee]qual [Oo]pportunity|"
    r"[Ee]qual [Oo]pportunity [Ee]mployer|EEO [Ss]tatement|EEO is the Law|"
    r"(?:[A-Z][\w&.\-]* )*provides equal employment opportunity|"
    r"Commitment to Diversity and Inclusion|Diversity,? [Ee]quity,? and Inclusion Statement|"
    r"Protecting Yourself from Recruitment|Recruitment [Ff]raud|Beware of|"
    r"We (?:do not|never) (?:ask|request) (?:for )?(?:payment|money|financial)|"
    r"Reasonable accommodations? (?:are|will|may|is)|"
    r"(?:We|This employer) participates? in E-?Verify|"
    r"Pay [Tt]ransparency|It is the policy of|"
    r"[Aa]ll employment decisions are|We are committed to (?:providing|creating) an? (?:accessible|inclusive)")
SPLIT_LEGAL = re.compile(r"(?<=[.!?])\s+(?=(?:" + LEGAL_OPEN + r"))")
# A node IS a notice (rather than merely mentioning one) when it OPENS as one. Anchored, so a
# requirements list whose last item ends "...is an Equal Opportunity Employer" is untouched --
# the exact regression split_boilerplate's docstring records.
LEGAL_STARTS = re.compile(r"\A\s*(?:" + LEGAL_OPEN + r")")


def _hyphen_list(t):
    """A hyphen-flattened list split into (lead, items), or None.

    Guarded exactly as jd_flat_list is, and for the same reason: one wrong boundary is worse than
    no split at all. Three items minimum, every item long enough to be a real one, and the whole
    run has to be long enough that a list is a plausible reading of it.
    """
    # A LENGTH FLOOR, but a low one: the load-bearing guards are the item COUNT and the item
    # LENGTH below, not this. At 240 it refused a genuine three-item requirements run of 180
    # characters while catching nothing extra.
    if len(t) < 160:
        return None
    parts = HYPHEN_RUN.split(t)
    if len(parts) < HYPHEN_MIN_ITEMS + 1:
        return None
    items = [p.strip() for p in parts[1:] if p.strip()]
    if len(items) < HYPHEN_MIN_ITEMS:
        return None
    # A SHORT ITEM MEANS A BAD CUT -- the same JD_ITEM_MIN judgement jd_flat_list makes. A date
    # range or a hyphenated aside inside prose produces one of these, and abandoning the whole
    # split is the right answer because the alternative is a fragment rendered as a bullet.
    if any(len(i) < JD_ITEM_MIN for i in items):
        return None
    return parts[0].strip(), items


# A LIST WHOSE ITEMS HAVE NO SEPARATOR AT ALL, recovered from a REPEATED OPENING PHRASE.
#
# This is the shape behind most of the remaining wall of text, and it is the one jd_flat_list
# cannot reach: that splits on JD_ITEM_START, a fixed vocabulary of words that open a
# requirement, and a real Microsoft posting opens four consecutive items with "Some exposure or
# working knowledge ..." -- perfectly regular, and not one of the sixty words on that list.
#
# REPETITION IS THE SIGNAL, AND IT NEEDS NO VOCABULARY. An employer writing a list writes the
# same opener each time; prose does not. That makes this complementary to jd_flat_list rather
# than a bigger version of it, and it generalises to boards nobody has looked at.
#
# THE STOPLIST IS WHAT KEEPS IT HONEST. "The company", "Our team" and "This role" repeat in
# ordinary prose constantly, so a phrase opening with a determiner or a pronoun is refused
# outright -- without it this splits narrative paragraphs into nonsense.
REPEAT_STOP = frozenset(
    "the this that these those our your their its we you they it he she a an and but or if as "
    "in on at by for to with from of is are was were be been has have had will would can could "
    "there here when while after before during".split())
REPEAT_OPEN = re.compile(r"(?<=[a-z0-9)\]])\s+([A-Z][A-Za-z]+(?:\s+[A-Za-z]+){1,3})\s")
REPEAT_MIN = 3


def _repeat_list(t):
    """(lead, items) for a run whose items all start the same way, or None."""
    if len(t) < 300:
        return None
    # EVERY SUB-PHRASE, NOT JUST THE WHOLE MATCH, and this is not tidiness -- without it the
    # first item of a list is routinely missed. The alternation is greedy, so on
    # "...concepts and Git Some exposure or working knowledge..." it captures "Git Some exposure
    # or" while the next three items capture "Some exposure or working". Those never align, the
    # count comes to three instead of four, and the first item stays welded to the lead-in.
    # Indexing each contiguous word run of the match, at that run's own offset, makes them meet.
    at = {}
    for m in REPEAT_OPEN.finditer(t):
        words, pos, base = m.group(1).split(), [], m.start(1)
        cur = base
        for w in words:
            cur = t.index(w, cur)
            pos.append(cur)
            cur += len(w)
        for i, w in enumerate(words):
            if w.lower() in REPEAT_STOP:
                continue
            for j in range(i + 2, len(words) + 1):
                at.setdefault(" ".join(words[i:j]).lower(), set()).add(pos[i])
    at = {k: sorted(v) for k, v in at.items()}
    if not at:
        return None
    # MOST OCCURRENCES WINS, then the longest phrase. A longer repeated opener is a stronger
    # claim about the shape than a shorter one that happens to tie.
    phrase, cuts = max(at.items(), key=lambda kv: (len(kv[1]), len(kv[0])))
    if len(cuts) < REPEAT_MIN:
        return None
    lead = t[:cuts[0]].strip()
    items = []
    for i, start in enumerate(cuts):
        end = cuts[i + 1] if i + 1 < len(cuts) else len(t)
        items.append(t[start:end].strip())
    # THE SAME JD_ITEM_MIN JUDGEMENT jd_flat_list AND _hyphen_list MAKE. A short piece means the
    # phrase also occurs inside one of the items, so the boundaries are wrong and the whole
    # split is abandoned rather than half-applied.
    if any(len(i) < JD_ITEM_MIN for i in items):
        return None
    return lead, items


# LIST ITEMS THAT OPEN WITH A NUMBER -- "3+ years managing CMS platforms.", "2-4 years
# developing software applications." -- which is how most requirements lists are written and
# which none of the existing splitters can see. jd_flat_list needs one of JD_ITEM_START's WORDS
# after the boundary, and "3+" is a digit; _repeat_list needs a repeated phrase, and each of
# these items opens with a different number. Reading the sections that still rendered as a wall,
# this was the commonest shape left in them.
NUMBER_ITEM = re.compile(
    r"(?<=[.!?])\s+(?=\d[\d,]*\s*(?:\+|\s*[-\u2013]\s*\d+|\s+to\s+\d+)?\s*(?:years?|yrs?)\b)",
    re.I)
# The same opener WITHOUT the sentence-boundary lookbehind, for the first item only -- see
# _number_list.
NUMBER_OPEN = re.compile(
    r"\d[\d,]*\s*(?:\+|\s*[-\u2013]\s*\d+|\s+to\s+\d+)?\s*(?:years?|yrs?)\b", re.I)
NUMBER_MIN_ITEMS = 3


def _number_list(t):
    """(lead, items) for a run of number-opening requirements, or None.

    Three items minimum, not two: "... in 2019. 5 years later the team ..." is prose, and one
    accidental boundary in a paragraph is far likelier than three.
    """
    if len(t) < 240:
        return None
    parts = NUMBER_ITEM.split(t)
    items = [p.strip() for p in parts[1:] if p.strip()]
    lead = parts[0].strip()
    # THE FIRST ITEM IS ROUTINELY STUCK INSIDE THE LEAD, because a list that starts straight
    # after its own heading has no sentence stop in front of it: "Basic Qualifications 3+ years
    # managing ... at scale. 2+ years of ..." has boundaries before the second and third items
    # and none before the first. Cutting there needs no sentence stop and is safe ONLY once two
    # anchored boundaries have already established that this run is a numbered list -- which is
    # exactly what the len(items) >= 2 gate says.
    if len(items) >= 2:
        m = NUMBER_OPEN.search(lead)
        if m and m.start() > 0:
            head, first = lead[:m.start()].strip(), lead[m.start():].strip()
            if len(first) >= JD_ITEM_MIN:
                lead, items = head, [first] + items
    if len(items) < NUMBER_MIN_ITEMS:
        return None
    if any(len(i) < JD_ITEM_MIN for i in items):
        return None
    return lead, items


# A HEADING STRANDED IN THE MIDDLE OF A SENTENCE, which is pass 1 promoting a phrase it had no
# business promoting. "The base salary range for this position is listed below" arrives as three
# nodes -- p("The base"), h("salary range"), p("for this position is listed below") -- because
# JD_SECTION's WEAK tier fires on "salary range" followed by a capital, and _soup_text's inline
# markup does the rest.
#
# MEASURED AT 37% OF DESCRIPTIONS, and it costs twice: the sentence renders as three blocks with
# a section heading through the middle of it, AND the fragment gets classified, so two words in
# the middle of a paragraph open a Compensation and Benefits section. Ten sampled cases were ten
# true positives.
#
# THE TEST IS GRAMMATICAL, not a vocabulary: the line before does not end a sentence and the line
# after starts lower case. A heading the employer actually wrote has a full stop in front of it
# and a capital after it, so this cannot reach one. Pass 1 is byte-frozen, so the repair is here.
_ENDS_SENTENCE = re.compile(r"[.!?:;]['\"\u2019\u201d)\]]*\s*\Z")


def _mend(nodes):
    """Rejoin a heading that pass 1 promoted from the middle of a sentence."""
    out = []
    i = 0
    while i < len(nodes):
        node = nodes[i]
        if (node[0] == "h" and out and out[-1][0] == "p"
                and i + 1 < len(nodes) and nodes[i + 1][0] == "p"
                and not _ENDS_SENTENCE.search(out[-1][1])
                and re.match(r"[a-z]", nodes[i + 1][1])):
            # The joined paragraph is a "p" again, so a run of stranded headings in one sentence
            # collapses on successive passes of this loop rather than needing its own case.
            out[-1] = ("p", "%s %s %s" % (out[-1][1], node[1], nodes[i + 1][1]))
            i += 2
            continue
        out.append(node)
        i += 1
    return out


def _emit_list(out, lead, items):
    """Place a recovered list and its lead-in, rather than dumping the lead-in as a paragraph.

    THE LEAD IS USUALLY A HEADING and throwing it away costs twice. "...requirements. Basic
    Qualifications 3+ years of ..." recovers three items and leaves "Basic Qualifications" in
    front of them; as a paragraph that is a two-word stub AND a section label nobody gets to use.
    Measured: it was the single commonest stubby fragment presplit produced, 16 occurrences in
    1,500 descriptions, all of them real headings.

    A SHORT LEAD THAT IS NOT A HEADING IS A BAD BOUNDARY. It is folded into the first item rather
    than left behind, because a slightly long bullet is better than a fragment on its own line.
    """
    if lead:
        if is_jd_heading(lead):
            out.append(("h", lead))
        elif len(lead) < JD_ITEM_MIN and items:
            items = [lead + " " + items[0]] + items[1:]
        else:
            out.append(("p", lead))
    out.append(("ul", items))


def presplit(nodes):
    """[node] -> [node]: same text, same order, possibly more nodes.

    The only pass allowed to change a node's boundaries. It both CUTS and JOINS -- the invariant
    is not "cuts only", it is that the text and its ORDER come out unchanged, which is what
    scripts/test_jdrender.py asserts and what makes every later pass safe.

    Runs BEFORE split_boilerplate, so the disclosure scanner sees the finer nodes too -- a legal
    notice welded onto the end of a paragraph is one it could not previously separate.
    """
    out = []
    # MEND BEFORE CUTTING. A sentence broken into three nodes cannot be read by any of the list
    # recoveries below -- they work on one node at a time -- and rejoining it first also gives
    # SPLIT_PHRASE a real sentence boundary to anchor on.
    for node in _mend(nodes):
        if node[0] != "p":
            out.append(node)
            continue
        last = 0
        pieces = []
        for m in SPLIT_PHRASE.finditer(node[1]):
            before = node[1][last:m.start()].strip()
            if before:
                pieces.append(("p", before))
            pieces.append(("h", m.group(1)))
            last = m.end()
        tail = node[1][last:].strip()
        if tail:
            pieces.append(("p", tail))
        for kind, val in (pieces or [node]):
            if kind != "p":
                out.append((kind, val))
                continue
            for part in SPLIT_LEGAL.split(val):
                part = part.strip()
                if not part:
                    continue
                # MOST SPECIFIC FIRST. An explicit separator beats a numeric opener beats a
                # repeated phrase; each is a weaker claim about the shape than the one before.
                hy = _hyphen_list(part) or _number_list(part) or _repeat_list(part)
                if not hy:
                    out.append(("p", part))
                    continue
                _emit_list(out, hy[0], hy[1])
    return out


def lift_legal(body, legal):
    """Move nodes that ARE a notice out of the body and into the disclosure.

    (body, legal) in, (body, legal) out. split_boilerplate stays byte-identical -- all five of
    its assertions in scripts/test_jdrender.py defend a real past bug, and its backward scan is
    right about the shape it handles. This is the mid-document case it deliberately cannot see:
    its own docstring explains that a FORWARD scan was unsafe when the order was the employer's,
    because a notice quoted mid-body under "Our Values" would swallow the rest of the document.
    That objection is answered rather than ignored -- this moves INDIVIDUAL nodes that open as a
    notice, never a run reaching to the end, so there is nothing for it to swallow.

    59% of uncollapsed notices sit mid-document, so the backward scan alone cannot reach them.

    THE SAME THREE ABORTS split_boilerplate USES, for the same reasons: never take everything,
    never exceed MAX_LEGAL_SHARE of the text, and never leave the body empty.
    """
    if not body:
        return body, legal
    keep, lifted = [], []
    for node in body:
        txt = _node_text(node)
        # A LIST HAS TO BE A NOTICE THROUGHOUT, the majority rule split_boilerplate already
        # needed: one stray "Equal Opportunity Employer" item once hid a 2,944 character
        # requirements list.
        if node[0] == "ul":
            items = [i for i in node[1] if i.strip()]
            isnotice = bool(items) and sum(1 for i in items if LEGAL_STARTS.search(i)) * 2 > len(items)
        else:
            isnotice = bool(LEGAL_STARTS.search(txt))
        (lifted if isnotice else keep).append(node)
    if not lifted or not keep:
        return body, legal
    total = sum(len(_node_text(n)) for n in body) + sum(len(_node_text(n)) for n in legal) or 1
    if (sum(len(_node_text(n)) for n in lifted)
            + sum(len(_node_text(n)) for n in legal)) / float(total) > MAX_LEGAL_SHARE:
        return body, legal
    return keep, lifted + list(legal)


# ---------------------------------------------------------------------------------------------
# Pass 2c: cut the description into runs, then regroup them into the canonical stack.
# ---------------------------------------------------------------------------------------------
# INFERENCE MAY ONLY FIRE UNDER A WEAK CUSTODIAN, and that one rule is the difference between
# this working and this being a liability. The custodian is whichever bucket the employer's last
# recognised heading put us in. Three of the eight are containers of last resort: an employer who
# wrote "About Lyra Health" and then twelve unlabelled paragraphs of duties and qualifications
# made no claim whatever about those twelve paragraphs, so inference is entitled to rescue them.
# An employer who wrote "Minimum Qualifications" DID make a claim, and inference has nothing to
# add to it -- under those five buckets it is switched off completely, so no list the employer
# labelled can ever be pulled apart by a guess.
_WEAK_CUSTODIAN = ("summary", "about", "other")


def _group(nodes):
    """[node] -> [[key, employer_heading, [nodes], inferred]] in DOCUMENT order.

    EVERY NODE IS PLACED EXACTLY ONCE and no node is copied, cut or rewritten: this is a
    partition of its input, which is what lets scripts/test_jdrender.py check conservation by
    identity rather than by string comparison.
    """
    runs = [["summary", "", [], False]]
    custodian = "summary"
    since_head = 0
    for node in nodes:
        if node[0] == "h":
            since_head = 0
            key = classify_heading(node[1])
            if key not in SEC_LABELS:
                # AN UNRECOGNISED HEADING INHERITS rather than opening "Additional Information".
                # "Season Details" under a job summary is part of that summary, and "legal" here
                # is a notice the employer wrote mid-document. Filing either under a catch-all
                # would scatter the employer's narrative to make our stack look fuller than the
                # posting actually is, which is the opposite of the point.
                #
                # EXCEPT FROM `about`, WHICH IS THE ONE BUCKET THAT LEAVES THE DESCRIPTION.
                # render_split lifts it to the page foot, so inheriting into it does not merely
                # mislabel a run, it MOVES it off the job. A posting opening "WHO WE ARE" and
                # continuing "YOU WILL" / "YOU HAVE" / "WE PREFER" -- none of which this module
                # recognises -- relocated in full: body empty, 2,699 characters rendered under
                # "About <Company>". Measured at 1,748 descriptions with a median 17% of the
                # text relocated and 89 postings moving more than half of themselves.
                key = "summary" if runs[-1][0] == "about" else runs[-1][0]
            else:
                custodian = key
            runs.append([key, node[1], [], False])
            continue
        # A HEADING ALWAYS GOVERNS AT LEAST ITS OWN FIRST NODE, and so does the start of the
        # document. Two bugs in one guard:
        #   * jd_samples[16] opens "Job Title: Senior Program and Project Manager ... Local
        #     candidate preferred", which votes `pref` on that one word -- and the whole posting
        #     rendered under Preferred Qualifications with no Overview above it. Whatever a
        #     description opens with is what it is about, whoever else wants to claim it.
        #   * without it, inference could take the ONE node under a heading and stranded that
        #     heading in an empty run. jd_samples[14] lost the words "Job Description" and [19]
        #     lost "Our Team" exactly that way, which the conservation check caught.
        if since_head and custodian in _WEAK_CUSTODIAN:
            key = infer_bucket(node)
            if key and key != runs[-1][0]:
                runs.append([key, "", [], True])
        runs[-1][2].append(node)
        since_head += 1
    # A RUN WITH A HEADING IS KEPT EVEN WHEN IT HOLDS NOTHING. Two headings in a row is a real
    # shape -- an employer writing "Job Description" immediately above "Position Summary" -- and
    # dropping the empty first run deleted the employer's words with it. Only a run that is
    # empty AND unnamed is nothing at all.
    return [r for r in runs if r[2] or r[1]]


def prepare(text_or_nodes):
    """(body, legal) for the sections path. THE ONE PIPELINE, and it is one function because it
    was briefly three copies of `lift_legal(*split_boilerplate(presplit(nodes)))` -- exactly the
    twin shape this project keeps getting caught by. jd_sections, render_jd and render_split all
    enter here, so they cannot drift, and scripts/test_jdrender.py can check the partition
    against the same objects the renderer will place.

    Order is load-bearing: presplit first, so the disclosure scanners see the finer nodes; then
    split_boilerplate for the trailing run it is built for; then lift_legal for the 59% of
    notices that sit mid-document where a backward scan cannot reach them.
    """
    nodes = (jd_nodes(text_or_nodes) if isinstance(text_or_nodes, str)
             else list(text_or_nodes or []))
    return lift_legal(*split_boilerplate(presplit(nodes)))


def jd_sections(text_or_nodes):
    """The canonical stack: [(key, [(employer_heading, [nodes], inferred), ...])] in SEC_ORDER.

    ONLY BUCKETS WITH CONTENT APPEAR. A posting that is nothing but an overview gets one
    section, not eight with seven apologies in them -- the owner's instruction, 2026-09-05. The
    page is standard because the sections that DO appear are always in the same order under the
    same labels, not because all eight are always drawn.
    """
    body, _legal = prepare(text_or_nodes)
    return _canon(_group(body))


def _canon(runs):
    """Regroup document-order runs into SEC_ORDER. STABLE, so the employer's sequence survives
    inside each bucket and only the buckets themselves move."""
    out = []
    for key in SEC_ORDER:
        mine = [(head, nodes, inf) for k, head, nodes, inf in runs if k == key]
        if mine:
            out.append((key, mine))
    return out


def _norm_label(t):
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def _groups_html(sections, hl):
    """The canonical stack -> HTML. The only place a section wrapper is written.

    OUR LABEL GOES IN A <span class="jdlabel">, and that is not decoration. It is chrome this
    renderer adds rather than anything the employer wrote, so the no-text-lost projection in
    scripts/test_jdrender.py strips it -- exactly the carve-out <summary> already has for the
    legal disclosure. Put our words anywhere else in the heading and every sectioned render
    reports as having invented text.

    AN ECHO IS HIDDEN, NEVER DROPPED. When the employer's own heading says what our label
    already says ("Responsibilities" under Responsibilities), rendering both stutters -- but
    deleting theirs is text loss and the conservation check would catch it. It stays in the DOM
    in a <span class="jdecho"> and the stylesheet hides it, which also spares a screen reader
    the same word twice.
    """
    out = []
    for key, runs in sections:
        label = SEC_LABELS[key]
        first = runs[0][0]
        attrs = 'data-sec="%s" id="jdsec-%s"' % (key, key)
        # INFERRED ONLY WHEN NO RUN IN THE BUCKET CARRIES AN EMPLOYER HEADING. A section the
        # employer named and inference merely extended is theirs, not ours.
        if all(inf for _h, _n, inf in runs):
            attrs += ' data-inferred="1"'
        head = '<span class="jdlabel">%s</span>' % esc(label)
        if first:
            head += ('<span class="jdecho">%s</span>' % esc(first)
                     if _norm_label(first) == _norm_label(label) else esc(first))
        out.append('<h4 class="jdh" %s>%s</h4>' % (attrs, head))
        for i, (hd, nodes, _inf) in enumerate(runs):
            if i and hd:
                out.append('<h5 class="jdsub">%s</h5>' % esc(hd))
            out.append(_nodes_html(nodes, hl, False))
    return "".join(out)


# ---------------------------------------------------------------------------------------------
# Pass 3: highlight the reader's keywords. Operates on PLAIN TEXT and returns plain-text runs.
# ---------------------------------------------------------------------------------------------
# Three, not four. With ten terms this bounds the page at thirty marks; a real Capital One posting
# hit thirty-six at four and started reading as a highlighter accident again. Showing where a term
# lives needs the first few occurrences, not all of them.
MAX_HITS_PER_TERM = 3
MIN_TERM_LEN = 3


class Highlighter(object):
    """Marks the terms the score was computed from, so the chips and the prose refer to the
    same words.

    Boundaries are lookarounds over [A-Za-z0-9], NOT \\b. \\b is wrong here and would be a
    silent failure: r"\\bc\\+\\+\\b" never matches "c++" at all, because "+" is not a word
    character, so every plus-suffixed and hash-suffixed skill would go unmarked. The lookaround
    form also mirrors core._term_present's own notion of "present", so the page and the score
    agree about what was found; it refuses "sql" inside "mysql" while still matching "c++" at
    "in c++." and "ci/cd" inside "(ci/cd)".
    """

    def __init__(self, have=(), missing=()):
        kinds = {}
        for t in (have or ()):
            k = (t or "").strip().lower()
            if len(k) >= MIN_TERM_LEN:
                kinds.setdefault(k, "have")
        for t in (missing or ()):
            k = (t or "").strip().lower()
            if len(k) >= MIN_TERM_LEN:
                kinds.setdefault(k, "miss")
        self.kinds = kinds
        self.hits = {}
        self.rx = None
        if kinds:
            # LONGEST FIRST. Python's "|" is leftmost-FIRST, not leftmost-longest, so without
            # this "project" would consume "project management" and "bi" would eat "power bi".
            terms = sorted(kinds, key=len, reverse=True)
            self.rx = re.compile(
                r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(t) for t in terms)
                + r")(?![A-Za-z0-9])", re.I)

    def runs(self, text):
        """[(kind, substring)] over `text`; kind is "" for the unmarked stretches.

        With no terms this is exactly [("", text)], which is what keeps the rendered output
        byte-identical to the frozen baseline when highlighting is off.
        """
        if not self.rx or not text:
            return [("", text)]
        out, pos = [], 0
        for m in self.rx.finditer(text):
            key = m.group(0).lower()
            kind = self.kinds.get(key)
            if not kind:
                continue
            if self.hits.get(key, 0) >= MAX_HITS_PER_TERM:
                continue
            self.hits[key] = self.hits.get(key, 0) + 1
            if m.start() > pos:
                out.append(("", text[pos:m.start()]))
            out.append((kind, m.group(0)))
            pos = m.end()
        if pos < len(text):
            out.append(("", text[pos:]))
        return out or [("", text)]


# ---------------------------------------------------------------------------------------------
# Render: nodes -> HTML. The only place a tag is written.
# ---------------------------------------------------------------------------------------------
def _text_html(text, hl):
    if hl is None:
        return esc(text)
    parts = []
    for kind, run in hl.runs(text):
        parts.append(esc(run) if not kind
                     else '<mark class="kw-%s">%s</mark>' % (kind, esc(run)))
    return "".join(parts)


def _nodes_html(nodes, hl, sections):
    out, anchored = [], set()
    # Consecutive metadata fields become ONE definition list. Emitting a <dl> per field would put
    # each label on its own block and reproduce the ladder this exists to remove.
    kind_at = [n[0] for n in nodes]
    in_dl = False
    for idx, (kind, val) in enumerate(nodes):
        if kind == "kv":
            if not in_dl:
                out.append('<dl class="jdkv">')
                in_dl = True
            label, vals = val
            out.append("<dt>%s</dt>" % esc(label))
            # Values ARE highlighted: on the posting that prompted this they are the skills
            # ("Java", "Amazon Web Services (AWS)"), which is exactly what the reader is scanning for.
            out.extend("<dd>%s</dd>" % _text_html(v, _hl_for(v, hl)) for v in vals)
            if idx + 1 >= len(kind_at) or kind_at[idx + 1] != "kv":
                out.append("</dl>")
                in_dl = False
            continue
        if kind == "h":
            sec = classify_heading(val) if sections else ""
            attr = ""
            if sec and sec != "legal":
                attr = ' data-sec="%s"' % sec
                # The FIRST heading in each bucket gets the anchor the jump strip targets. Only
                # the first: a description with three "Preferred Qualifications" style headings
                # would otherwise emit the same id three times, and a duplicate id makes the
                # link land on whichever the browser happens to prefer.
                if sec not in anchored:
                    anchored.add(sec)
                    attr += ' id="jdsec-%s"' % sec
            # A heading is the employer's own label. Never highlighted: a <mark> inside an <h4>
            # reads as emphasis on the section rather than on a skill.
            out.append('<h4 class="jdh"%s>%s</h4>' % (attr, esc(val)))
        elif kind == "p":
            out.append("<p>%s</p>" % _text_html(val, _hl_for(val, hl)))
        elif kind == "ul":
            out.append("<ul>%s</ul>"
                       % "".join("<li>%s</li>" % _text_html(i, _hl_for(i, hl)) for i in val))
    return "".join(out)


def text_halves(text):
    """(body, legal) as two lowercased strings: the description minus its notices, and the notices.

    Exposed because the SCORER reads the whole description, boilerplate included, so its keyword
    list can contain "regarding criminal", "background inquiries" and "applicable federal". A
    caller can ask "is this term in the notices and nowhere else", which is a property of this
    posting rather than a blacklist somebody has to maintain.

    BOTH halves are built from the same jd_nodes() pass. Returning only the legal half and letting
    the caller subtract it from the raw JD does not work, and failed silently the first time it was
    tried: node text is whitespace-normalised and re-joined, so the reconstructed string never
    appears verbatim in the original, str.replace removed nothing, and every term looked as though
    it occurred outside the notices.

    Covers boilerplate wherever it sits, not only a collapsible run at the end.
    """
    body, legal = [], []
    for kind, val in jd_nodes(text):
        # A "ul" node's val is a list of items and a "kv" node's is (label, [values]) -- see
        # _node_text. `val if kind == "ul" else [val]` covered the first and not the second, so a
        # kv node handed _LEGAL_BODY.search a TUPLE and raised TypeError on 0.3% of stored
        # descriptions -- the ones carrying a metadata header ("Clearance Level: None",
        # "Category: Software Engineering"). web._useful_terms calls this inside a bare except, so
        # the failure was swallowed and it carried on with an EMPTY legal half, silently
        # switching off the one rule that keeps "regarding criminal" out of the keywords it
        # advises adding to a resume. Both halves are tested per RUN, so the kv label and each of
        # its values are judged separately rather than as one joined string.
        if kind == "kv":
            runs = [val[0]] + list(val[1])
        elif kind == "ul":
            runs = list(val)
        else:
            runs = [val]
        for run in runs:
            hit = _LEGAL_BODY.search(run) or (kind == "h" and _LEGAL_HEAD.search(run))
            (legal if hit else body).append(run)
    return " \n ".join(body).lower(), " \n ".join(legal).lower()


def _hl_for(text, hl):
    """`hl`, unless this run is legal boilerplate, in which case nothing is marked.

    Boilerplate is still scored (analyze_jd reads the whole description), so the term list can
    contain "regarding criminal", "background inquiries" and "applicable federal". Marking those
    as keywords worth adding is not merely noisy, it is advice to put "regarding criminal" on a
    résumé. Suppressing by SHAPE rather than by maintaining a word blacklist means a notice this
    module has never seen is covered too, and it reuses the pattern the collapser already needs.
    """
    if hl is None or not text:
        return hl
    return None if _LEGAL_BODY.search(text) else hl


def render_jd(text, have=(), missing=(), sections=True):
    """The whole pipeline. `sections=False, have=(), missing=()` reproduces the frozen bytes.

    THE TWO PATHS DIVERGE COMPLETELY NOW and the early return below is what guarantees it:
    `sections=False` never reaches the canonical stack, so pass 1 plus _nodes_html is the whole
    of it and scripts/fixtures/jd_html_baseline.json cannot move. Everything pass 2 does --
    reordering, our labels, the inference tier -- is on the other side of that branch.

    Degradation ladder, in order, and every rung is asserted by scripts/test_jdrender.py:
      1. No text at all -> "". The caller shows its own sentence rather than an empty .jd box.
      2. Text but no recognisable structure -> ONE section, Overview, holding exactly pass 1's
         blocks. Not eight sections explaining what the posting did not contain.
      3. Boilerplate run covering everything -> not collapsed.
      4. Text is NEVER dropped and never duplicated, whatever happens above. Order is no longer
         preserved, so the guard is a multiset comparison rather than a string one.
    """
    nodes = jd_nodes(text)
    if not nodes:
        return ""
    hl = Highlighter(have, missing) if (have or missing) else None
    if not sections:
        return _nodes_html(nodes, hl, False)
    nodes, legal = prepare(nodes)
    html = _groups_html(_canon(_group(nodes)), hl)
    if legal:
        # Title Case: it is a control, not prose. The employer's notices are not highlighted and
        # not relabelled, just moved behind a disclosure and left intact.
        html += ('<details class="jdlegal"><summary>Legal and Equal Opportunity Notices'
                 "</summary>%s</details>" % _nodes_html(legal, None, False))
    return html


def section_text(runs):
    """The plain text of one bucket's runs, for a caller that wants to READ a section rather
    than render it.

    Headings are excluded, and that is not tidiness: they are labels, and running a keyword
    extractor over them is how "Qualifications" and "Requirements" end up in a list of skills
    the page advises putting on a resume.
    """
    return " ".join(_node_text(n) for _h, nodes, _i in runs for n in nodes)


def render_split(text, have=(), missing=()):
    """(body_html, about_html, jumps) -- the canonical stack with the company blurb lifted out.

    THE ONE SECTION THAT DOES NOT RENDER WITH THE OTHERS. /job already carries an
    "About <Company>" block at the foot of the page, holding the research crawl and the open-role
    count, and the employer's own blurb belongs there rather than in front of the job. A third of
    postings open with three paragraphs about the company; the reader came for the posting.

    ONE PARSE, THREE ANSWERS. job_page used to call render_jd and jump_sections separately, which
    walked the description twice and would now do the section work twice as well.

    render_jd is untouched and still returns everything in one string, so the byte-freeze and
    every other caller are unaffected.
    """
    nodes = jd_nodes(text)
    if not nodes:
        return "", "", []
    hl = Highlighter(have, missing) if (have or missing) else None
    nodes, legal = prepare(nodes)
    secs = _canon(_group(nodes))
    body = [(k, runs) for k, runs in secs if k != "about"]
    html = _groups_html(body, hl)
    if legal:
        html += ('<details class="jdlegal"><summary>Legal and Equal Opportunity Notices'
                 "</summary>%s</details>" % _nodes_html(legal, None, False))
    # NO CANONICAL LABEL on the about half -- it renders under the page's own "About <Company>"
    # h2 and a second heading saying the same thing is noise. THE EMPLOYER'S OWN HEADINGS DO
    # RENDER, and leaving them out was silent text loss: _group keeps a heading in its run's
    # LABEL rather than as a node, so a comprehension over the run's NODES drops it. Measured
    # before the fix: 5,850 heading strings gone across 4,351 of 45,755 cached descriptions,
    # including whole "Job Title:" / "Job Location:" blocks. The conservation check in
    # scripts/test_jdrender.py did not catch it because it only ever projected render_jd.
    about = []
    for key, runs in secs:
        if key != "about":
            continue
        for head, ns, _inf in runs:
            if head:
                about.append('<h5 class="jdsub">%s</h5>' % esc(head))
            about.append(_nodes_html(ns, hl, False))
    return html, "".join(about), _jumps(body)


def _jumps(sections):
    """The anchor strip for a rendered stack. ONE DEFINITION, used by both entry points.

    render_split and jump_sections both answer "which links go in the strip", and having them
    answer it separately is the twin shape this project keeps getting caught by -- they would
    drift the first time either grew a rule.

    ONE LINK IS NOT NAVIGATION. A posting that resolves to a single section gets no strip rather
    than one link pointing at the top of the thing you are already reading.

    `about` IS NEVER LINKED, wherever this is called from. render_split renders that bucket under
    the page's own About heading and emits no #jdsec-about anchor for it, so offering the link
    would be a strip entry that goes nowhere. Excluding it HERE rather than in render_split is
    what keeps this function's promise true for jump_sections too -- the two used to disagree.
    """
    shown = [k for k, _runs in sections if k != "about"]
    return [(k, SEC_LABELS[k]) for k in shown] if len(shown) > 1 else []


def jump_sections(text_or_nodes):
    """[(key, label)] for the sections that actually rendered, in SEC_ORDER, for the anchor strip.

    EXACTLY THE SECTIONS ON THE PAGE, in exactly their order, because it is built from the same
    pass that draws them. It used to be built from a second walk looking only at headings, which
    could disagree with the page in both directions.

    NOT USED BY /job, which goes through render_split to get the strip and the markup from one
    parse. Kept as the standalone answer for any caller that wants the strip without the render,
    and it shares _jumps with render_split so the two cannot disagree.
    """
    return _jumps(jd_sections(text_or_nodes))
