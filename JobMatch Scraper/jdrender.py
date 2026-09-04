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
# Pass 2: label the sections. NOTHING IS REORDERED.
# ---------------------------------------------------------------------------------------------
# Reordering a description risks putting Requirements above the role summary and misrepresents
# what the employer wrote. And the employer's own heading TEXT is kept: "Basic Qualifications"
# and "Preferred Qualifications" both classify as requirements but mean must-have versus
# nice-to-have, and overwriting either with "Requirements" is information loss. The bucket goes
# on a data-sec attribute and drives the styling and the jump strip instead.
# SIX BUCKETS, AND TWO OF THEM ARE SPLITS OF THE ORIGINAL THREE.
#
# ORDER IS THE ALGORITHM: classify_heading returns the FIRST match, so a bucket that is a
# special case of another has to be listed above it. "Preferred Qualifications" contains both
# "preferred" and "qualificat"; pref is above req, so it lands in pref. "Basic Qualifications"
# has no preferred word and falls through to req.
#
# WHY SPLIT req. The old comment on this block said it out loud and then did not act on it:
# "'Basic Qualifications' and 'Preferred Qualifications' both classify as requirements but mean
# must-have versus nice-to-have". One bucket meant the jump strip said REQUIREMENTS once and a
# reader could not tell which of the two a ten-year number came from -- which is the difference
# between a job they cannot apply for and one they can. The parser has separated hard from soft
# floors since 704a290; this is the display finally carrying the same distinction.
#
# WHY SPLIT summary. "About the job", "Overview" and "Job Summary" were inside resp, so the
# strip labelled a role summary RESPONSIBILITIES. Measured over 3,000 cached descriptions, 26.4%
# carry a summary-style heading, so this is not a rare shape.
_SEC = [
    ("summary", re.compile(
        r"about (?:the|this|our) (?:role|job|position|opportunity|team)|about us|"
        r"job (?:summary|description|details|overview)|position (?:purpose|summary|overview)|"
        r"role (?:summary|overview)|overview|(?:the|your) (?:role|opportunity|impact)|"
        r"who we are|the position", re.I)),
    ("resp", re.compile(
        r"responsibilit|what you.?ll do|what you will do|essential (?:functions?|duties)|"
        r"duties|day in the life|key responsibilities|primary responsibilities|"
        r"what you.?ll be doing|in this role you", re.I)),
    ("pref", re.compile(
        r"preferred|preferable|nice to have|nice-to-have|desired|desirable|"
        r"additional qualificat|bonus|a plus|good to have|pluses", re.I)),
    ("req", re.compile(
        r"requirement|qualificat|what you.?ll (?:need|bring)|what you bring|"
        r"what we.?re looking for|who you are|required skills|skills|experience|education|"
        r"minimum|basic|must have|must-have", re.I)),
    ("ben", re.compile(
        r"benefit|perk|compensation|(?:pay|salary|compensation) range|what we offer|"
        r"why join|total rewards", re.I)),
]
_LEGAL_HEAD = re.compile(
    r"equal (?:employment )?opportunity|eeo|e-?verify|affirmative action|"
    r"reasonable accommodation|drug.?(?:free|screen)|background check|pay transparency|"
    r"applicant (?:privacy|rights)|fair chance|export control|itar|at.?will", re.I)
_LEGAL_BODY = re.compile(
    r"equal opportunity employer|with(?:out)? regard to race|regardless of race|"
    r"protected veteran|reasonable accommodation|drug.?free workplace|"
    r"criminal (?:history|background)|pay transparency|applicants? with disabilit", re.I)
SEC_LABELS = {"summary": "About the Role", "resp": "Responsibilities",
              "req": "Required", "pref": "Preferred", "ben": "Benefits"}
# THE ONE PLACE THE PORTAL IMPOSES ITS OWN ORDER. The PROSE is never reordered -- jdrender's
# rule at the top of this section still holds, and putting Requirements above a role summary
# would misrepresent what the employer wrote. The JUMP STRIP is different: it is the portal's
# own furniture, and a strip whose links appear in a different order on every job is a strip
# nobody learns. Google's scraped page puts benefits above qualifications, so in document order
# its strip opened with BENEFITS.
SEC_ORDER = ("summary", "resp", "req", "pref", "ben")


def classify_heading(t):
    """resp | req | ben | legal | "" for one heading's own words."""
    if _LEGAL_HEAD.search(t or ""):
        return "legal"
    for key, rx in _SEC:
        if rx.search(t or ""):
            return key
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

    Degradation ladder, in order, and every rung is asserted by scripts/test_jdrender.py:
      1. No text at all -> "". The caller shows its own sentence rather than an empty .jd box.
      2. Text but no recognisable structure -> exactly pass 1's blocks, byte for byte what
         app.js's jdHTML() produced. This is what makes "degrades to the old behaviour" a
         checkable claim instead of a promise.
      3. Boilerplate run covering everything -> not collapsed.
      4. Text is NEVER dropped, whatever happens above.
    """
    nodes = jd_nodes(text)
    if not nodes:
        return ""
    hl = Highlighter(have, missing) if (have or missing) else None
    legal = []
    if sections:
        nodes, legal = split_boilerplate(nodes)
    html = _nodes_html(nodes, hl, sections)
    if legal:
        # Title Case: it is a control, not prose. The employer's notices are not highlighted and
        # not relabelled, just moved behind a disclosure and left intact.
        html += ('<details class="jdlegal"><summary>Legal and Equal Opportunity Notices'
                 "</summary>%s</details>" % _nodes_html(legal, None, False))
    return html


def jump_sections(text_or_nodes):
    """[(key, label)] for the sections actually present, in SEC_ORDER, for the anchor strip.

    Returns only buckets that exist, so a description with no headings gets no strip rather than
    five dead links. Ordered by SEC_ORDER rather than by the document — see the note there.
    """
    nodes = (jd_nodes(text_or_nodes) if isinstance(text_or_nodes, str) else text_or_nodes) or []
    nodes, _legal = split_boilerplate(nodes)
    seen = set()
    for kind, val in nodes:
        if kind == "h":
            sec = classify_heading(val)
            if sec in SEC_LABELS:
                seen.add(sec)
    return [(s, SEC_LABELS[s]) for s in SEC_ORDER if s in seen]
