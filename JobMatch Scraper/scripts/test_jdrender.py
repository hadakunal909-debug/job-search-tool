#!/usr/bin/env python3
"""jdrender.py reproduces the retired JS renderer, and its additions never lose text.

Replaces scripts/test_jd_render.py, which diffed static/app.js's jdHTML() against its TypeScript
twin web/src/feed/jd.ts. Both are gone: the modal was the only caller of the first and NOTHING
imported the second, so that test compared two implementations neither of which shipped.

WHAT THIS TRADES, said plainly. The old test was explicitly not a snapshot test, on the correct
argument that a snapshot only proves self-consistency. scripts/fixtures/jd_html_baseline.json IS
a snapshot. That is legitimate here only because the second implementation was removed in the
same change, so there is nothing left to differentially test against. Its claim is "the port to
Python lost nothing", not "the algorithm is right". Regenerating it needs a reviewed diff.

The checks that are NOT snapshots, and carry the weight:
  * no text is dropped, over an alphanumeric projection, on every path
  * the sectioniser is not a silent no-op
  * highlighting off is byte-identical to highlighting absent
  * keyword boundaries: sql does not match inside mysql, c++ and ci/cd do match at punctuation
  * escaping holds under hostile input

    python scripts/test_jdrender.py
"""
import io
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import jdrender

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "jd_samples.json")
BASELINE = os.path.join(HERE, "fixtures", "jd_html_baseline.json")

FAILS = []


def check(name, cond, extra=""):
    if not cond:
        FAILS.append(name)
    print("  %s %-56s %s" % ("ok " if cond else "FAIL", name, extra))


samples = json.load(io.open(FIXTURE, encoding="utf-8"))
base = json.load(io.open(BASELINE, encoding="utf-8"))
frozen = base["html"]

print("=" * 92)
print("%d real descriptions; baseline captured from commit %s"
      % (len(samples), base.get("_captured_from_commit")))
print("=" * 92)

# ---------------------------------------------------------------------------------------------
print("\nFIDELITY: sections and highlighting off reproduce the retired renderer byte for byte")
by_shape, mismatch = {}, 0
for s, want in zip(samples, frozen):
    got = jdrender.render_jd(s["jd"], sections=False)
    ok = got == want
    tally = by_shape.setdefault(s["shape"], [0, 0])
    tally[1] += 1
    tally[0] += ok
    if not ok and mismatch < 3:
        mismatch += 1
        print("    MISMATCH  %s  %s" % (s["shape"], (s.get("title") or "")[:44]))
        for k in range(min(len(got), len(want))):
            if got[k] != want[k]:
                lo = max(0, k - 55)
                print("      at char %d" % k)
                print("      frozen: ...%s" % want[lo:k + 55].replace("\n", "\\n"))
                print("      python: ...%s" % got[lo:k + 55].replace("\n", "\\n"))
                break
        else:
            print("      identical for %d chars, lengths %d vs %d"
                  % (min(len(got), len(want)), len(want), len(got)))
for shape in sorted(by_shape):
    got, tot = by_shape[shape]
    check("shape %s" % shape, got == tot, "%d/%d" % (got, tot))

# A comparison of two empty strings is agreement on nothing. Carried over from the retired test.
check("every sample produced real markup", all(len(h) >= 40 for h in frozen),
      "shortest %d chars" % min(len(h) for h in frozen))
check("the corpus exercises lists, headings and paragraphs",
      sum(h.count("<li>") for h in frozen) >= 50 and sum(h.count("<h4") for h in frozen) >= 10,
      "%d li, %d h4, %d p" % (sum(h.count("<li>") for h in frozen),
                              sum(h.count("<h4") for h in frozen),
                              sum(h.count("<p>") for h in frozen)))


# ---------------------------------------------------------------------------------------------
def alnum(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def words(s):
    return re.findall(r"[a-z0-9]+", (s or "").lower())


# OUR SECTION LABELS ARE CHROME, exactly as <summary>'s "Legal and Equal Opportunity Notices" is,
# so a projection meant to prove the EMPLOYER's words survived has to drop them.
#
# BUILT FROM THE VOCABULARY, NOT FROM A WILDCARD. A `<span class="jdlabel">.*?</span>` stripper
# would silently swallow anything that ever leaked into that element and hide the regression it
# exists to catch. This can only ever remove one of the eight strings jdrender itself declares;
# a jdlabel holding anything else survives into the projection and fails the check.
_CHROME = re.compile(
    r'<span class="jdlabel">(?:%s)</span>'
    % "|".join(re.escape(jdrender.esc(v))
               for v in sorted(set(jdrender.SEC_LABELS.values()), key=len, reverse=True)))


def unchrome(html):
    return _CHROME.sub(" ", html or "")


def text_of(html):
    """Visible text: drop tags, then un-escape the four entities esc() writes.

    The <summary> label is dropped WITH its contents, because "Legal and Equal Opportunity
    Notices" is chrome this renderer adds rather than anything the employer wrote. Leaving it in
    made the projection 31 characters longer than the input and read as text loss inverted.
    """
    t = unchrome(html)
    t = re.sub(r"<summary>.*?</summary>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return (t.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&amp;", "&"))


print("\nNO TEXT IS DROPPED, on any path")
# THE PLAIN PATH IS STILL AN EXACT, ORDER-SENSITIVE STRING COMPARISON, and it has to be: nothing
# reorders on that path, so anything weaker would give up force for no reason.
bad = []
for s in samples:
    if alnum(text_of(jdrender.render_jd(s["jd"], sections=False))) != alnum(s["jd"]):
        bad.append(s.get("title") or "?")
check("all %d samples keep every character (plain)" % len(samples), not bad,
      "" if not bad else "lost text in %d: %s" % (len(bad), bad[:2]))

# THE SECTIONED PATH REORDERS ON PURPOSE (see jdrender Pass 2), so an ordered comparison would
# now fail on every sample and prove nothing. It is replaced by two checks that are together
# STRICTLY STRONGER than the one they replace:
#
#   CONSERVATION -- a multiset, not a set. Order-insensitive, but it still catches a word
#   dropped AND a word duplicated, which is the failure an ordered string compare would report
#   as "lost text" and a set would not see at all.
bad = []
for s in samples:
    html = jdrender.render_jd(s["jd"], have=["python", "sql", "aws"], missing=["kubernetes"])
    if Counter(words(text_of(html))) != Counter(words(s["jd"])):
        bad.append(s.get("title") or "?")
check("all %d samples conserve every word (sectioned + highlighted)" % len(samples), not bad,
      "" if not bad else "changed text in %d: %s" % (len(bad), bad[:2]))

#   PERMUTATION -- checked at the level where the permutation actually happens, and BY IDENTITY.
#   jd_sections must PARTITION its input: every content node placed exactly once, none copied,
#   none rebuilt, none dropped. That is what makes "the reorder cannot lose text" a structural
#   property of the code rather than something the word count happens to agree with today.
#
#   HEADING NODES ARE COUNTED SEPARATELY because they do not travel as nodes: _group lifts each
#   one into its run's LABEL and _groups_html re-emits it. That is not a loophole -- it is the
#   thing that went wrong first. Inference took the single node under a heading, left the run
#   empty, and the empty-run filter deleted the heading's words with it. So the labels are
#   asserted to survive as a multiset too, and both halves have to hold.
bad, lost = [], []
for s in samples:
    # THE SAME PIPELINE THE RENDERER RUNS, entered at the same door. Checking identity against a
    # separately-built node list cannot work: presplit rebuilds the nodes it cuts, so two calls
    # produce equal-but-not-identical tuples and the check would be vacuous or wrong.
    body, _legal = jdrender.prepare(s["jd"])
    secs = jdrender._canon(jdrender._group(body))
    placed = [n for _k, runs in secs for _h, ns, _i in runs for n in ns]
    content = [n for n in body if n[0] != "h"]
    if len(placed) != len(content) or not all(
            sum(1 for p in placed if p is n) == 1 for n in content):
        bad.append(s.get("title") or "?")
    heads = [h for _k, runs in secs for h, _n, _i in runs if h]
    if Counter(heads) != Counter(n[1] for n in body if n[0] == "h"):
        lost.append(s.get("title") or "?")
check("every content node is placed exactly once, by identity", not bad,
      "" if not bad else "%d samples: %s" % (len(bad), bad[:2]))
check("every employer heading survives as a run label", not lost,
      "" if not lost else "%d samples: %s" % (len(lost), lost[:2]))

# The same projection against the FROZEN bytes. If this fails, the shipped JS renderer was
# dropping text too, and that is a live bug rather than a porting one.
lossy = [s.get("title") for s, h in zip(samples, frozen) if alnum(text_of(h)) != alnum(s["jd"])]
check("the retired JS renderer also kept every character", not lossy,
      "" if not lossy else "pre-existing loss in: %s" % lossy[:3])

# ---------------------------------------------------------------------------------------------
print("\nTHE SECTIONISER IS NOT A NO-OP")
secd = [jdrender.render_jd(s["jd"]) for s in samples]
check("some samples carry a data-sec bucket",
      sum('data-sec="' in h for h in secd) >= 3,
      "%d of %d" % (sum('data-sec="' in h for h in secd), len(secd)))
# Asserted on a SYNTHETIC posting, not on the fixture. Whether 20 sampled descriptions happen to
# end in a prose EEO block is a property of the fixture, not of this code, and an assertion on it
# would push the next person to loosen the guard until reality matched the test. The real corpus
# count is reported instead, and what is asserted is that the mechanism fires when its shape
# actually occurs, and that the corpus loses no text (checked above).
# The body has to outweigh the notice, because MAX_LEGAL_SHARE deliberately refuses to collapse
# a run that is most of the document. The first version of this fixture was 74% boilerplate and
# was rejected by that guard, correctly.
WITH_EEO = ("The Role\n"
            "Build and ship the payments service that moves several billion dollars a year. You "
            "will own it end to end, from the schema through the public API to the dashboards the "
            "finance team reads every morning.\n\n"
            "Requirements:\n"
            "- Five years of Python in production, with real ownership of a service\n"
            "- Strong SQL and a working understanding of transaction isolation\n"
            "- Experience operating something that cannot go down quietly\n\n"
            "What We Offer\n"
            "A small team, a short deploy pipeline, and budget for the tools you need to do the "
            "work properly rather than the cheapest ones that technically qualify.\n\n"
            "Equal Opportunity Employer\n"
            "We consider all qualified applicants without regard to race, colour, religion, sex, "
            "national origin, disability or protected veteran status.\n"
            "Reasonable accommodation is available to applicants with disabilities on request.")
eeo = jdrender.render_jd(WITH_EEO)
check("a trailing prose EEO block IS collapsed", "jdlegal" in eeo)
head, _, tail = eeo.partition("<details")
check("the collapsed block keeps the role content visible",
      "payments service" in head and "Five years of Python" in head)
check("the notice text survives inside the disclosure", "protected veteran status" in tail)
# The regression that made the ul rule necessary: one stray notice in a long requirements list.
STRAY = ("Responsibilities:\n"
         + "\n".join("- Build subsystem number %d to specification and review it" % n
                     for n in range(1, 9))
         + "\n- Company is an Equal Opportunity Employer")
check("a requirements list with ONE stray notice is not collapsed",
      "jdlegal" not in jdrender.render_jd(STRAY),
      "this hid a 2,944 character list behind the disclosure before the majority rule")
print("     (real corpus: %d of %d descriptions collapse a disclosure)"
      % (sum("jdlegal" in h for h in secd), len(secd)))
check("jump_sections returns only buckets that exist",
      all(all(k in jdrender.SEC_LABELS for k, _l in jdrender.jump_sections(s["jd"]))
          for s in samples))
check("a description with no headings gets no jump strip",
      # Deliberately avoids the words "about the job", "summary", "role" and friends: those ARE
      # section names and jd_paragraphs promotes them mid-sentence on purpose, so the first
      # attempt at this case ("Just one sentence about the job...") legitimately produced a
      # Responsibilities bucket and the assertion was wrong rather than the code.
      jdrender.jump_sections("We need somebody who can ship quickly and talk to customers.") == [])
# A run of boilerplate that IS the whole document must not be collapsed to nothing.
allboiler = ("Equal Opportunity Employer\nWe consider all applicants without regard to race, "
             "colour, religion or protected veteran status. Reasonable accommodation is "
             "available on request.")
check("all-boilerplate description is not collapsed", "jdlegal" not in jdrender.render_jd(allboiler),
      "a JD that renders as one closed <details> looks broken")
check("a single short notice stays inline",
      "jdlegal" not in jdrender.render_jd("The Role\nBuild things.\nEqual Opportunity Employer."))
check("empty input renders nothing at all", jdrender.render_jd("") == ""
      and jdrender.render_jd(None) == "")

# ---------------------------------------------------------------------------------------------
print("\nRENDER_SPLIT: THE TWO HALVES TOGETHER STILL HOLD EVERYTHING")
# THIS CHECK EXISTS BECAUSE ITS ABSENCE SHIPPED A BUG. render_split lifts the `about` bucket out
# of the body so /job can render it under its own About <Company> heading, and the first version
# built that half by walking each run's NODES -- which is not where _group keeps a heading. It
# keeps it in the run's LABEL. Every employer heading in that bucket was therefore deleted from
# the page: measured at 5,850 heading strings across 4,351 of 45,755 cached descriptions,
# including whole "Job Title:" and "Job Location:" blocks.
#
# The conservation check above did not see it because it only ever projected render_jd, and
# `grep -c render_split` over this file returned 0. A second entry point needs its own guard.
bad, empty = [], []
for s in samples:
    body, about, jumps = jdrender.render_split(s["jd"])
    if Counter(words(text_of(body) + " " + text_of(about))) != Counter(words(s["jd"])):
        bad.append(s.get("title") or "?")
    # THE BODY MUST NOT BE EMPTY WHILE THE ABOUT HALF IS FULL. That is the whole description
    # relocated to the foot of the page under About, leaving "Job Description" over an empty box.
    if about.strip() and not body.strip():
        empty.append(s.get("title") or "?")
check("render_split conserves every word across BOTH halves", not bad,
      "" if not bad else "%d samples: %s" % (len(bad), bad[:2]))
check("render_split never empties the body into the about half", not empty,
      "" if not empty else "%d samples: %s" % (len(empty), empty[:2]))

# The two shapes that produced the bug, as explicit fixtures rather than only via the corpus.
ABOUT_HEADED = ("About Us\n"
                "We build payments infrastructure and we have done since 2011.\n\n"
                "Our Mission\n"
                "To make moving money boring, everywhere, for everyone who has to do it.\n\n"
                "Responsibilities\n"
                "- Own the ledger pipeline end to end\n"
                "- Partner with the finance team every week\n")
b, a, j = jdrender.render_split(ABOUT_HEADED)
check("the employer's headings inside the about half still render",
      "About Us" in a and "Our Mission" in a,
      "_group keeps a heading in the run's LABEL, not as a node")
check("...and the role content stays in the body", "ledger pipeline" in b)
check("the jump strip never offers a link to the about anchor",
      all(k != "about" for k, _l in j) and "jdsec-about" not in b,
      "render_split emits no anchor for it, so the link would go nowhere")
check("jump_sections agrees with render_split about that",
      all(k != "about" for k, _l in jdrender.jump_sections(ABOUT_HEADED)),
      "the two used to disagree: one included about, the other did not")

# An unrecognised heading must never inherit `about`, because `about` leaves the description.
WHO_WE_ARE = ("WHO WE ARE\n"
              "We are a robotics company working on autonomous freight.\n\n"
              "YOU WILL\n"
              "- Own the perception stack end to end for the platform\n"
              "- Partner with hardware on sensor placement each quarter\n\n"
              "YOU HAVE\n"
              "- Five years of production C++ in a robotics setting\n"
              "- Strong grounding in sensor fusion and calibration\n")
b2, a2, _j2 = jdrender.render_split(WHO_WE_ARE)
check("an unrecognised heading does not inherit `about` and get relocated",
      "perception stack" in b2 and "production C++" in b2,
      "1,748 descriptions moved a median 17% of themselves to the page foot this way")
check("...while the real company blurb still does move", "autonomous freight" in a2)


# ---------------------------------------------------------------------------------------------
print("\nPRESPLIT: RESHAPES BOUNDARIES, PRESERVES TEXT AND ORDER")
# THE INVARIANT THAT MAKES EVERYTHING DOWNSTREAM SAFE. presplit is the only pass allowed to move
# a node boundary; if its output is the same text in the same order, then no later pass can lose
# text because no later pass cuts anything. Asserted IN ORDER (unlike the sectioned conservation
# check further up), because presplit does not reorder and a weaker claim here would be a gift.
bad = []
for s in samples:
    nodes = jdrender.jd_nodes(s["jd"])
    cut = jdrender.presplit(nodes)
    if alnum(" ".join(map(jdrender._node_text, cut))) != \
            alnum(" ".join(map(jdrender._node_text, nodes))):
        bad.append(s.get("title") or "?")
check("presplit preserves every character, in document order", not bad,
      "" if not bad else "%d samples: %s" % (len(bad), bad[:2]))
check("presplit is idempotent", all(
    jdrender.presplit(jdrender.presplit(jdrender.jd_nodes(s["jd"])))
    == jdrender.presplit(jdrender.jd_nodes(s["jd"])) for s in samples),
    "a second pass must find nothing left to cut")

# SPLIT_PHRASE: the trap that cost the most during development.
for text, want, why in (
        ("We will be clear about role expectations. What You Need to Have Bachelor degree.",
         True, "sentence-initial, Title Case, followed by a capital"),
        ("You will translate requirements into actionable plans for the team.",
         False, "MID-SENTENCE: cutting here leaves a dangling half-sentence"),
        ("The company offers benefits to full time employees who qualify today.",
         False, "same, on the word 'benefits'"),
        ("He has many responsibilities. responsibilities are shared here.",
         False, "lower case: prose, not a heading"),
):
    got = bool(jdrender.SPLIT_PHRASE.search(text))
    check("SPLIT_PHRASE %-5s %r" % (want, text[:44]), got == want, why)

# The three list recoveries, each with the guard that stops it firing on prose.
HYPHEN_OK = ("Basic qualifications - 5+ years of experience building and operating "
             "distributed systems - 3+ years of experience with Java or Python in "
             "production - 2+ years leading engineering teams and mentoring juniors")
check("a hyphen-flattened list is recovered", (jdrender._hyphen_list(HYPHEN_OK) or (0, []))[1])
check("a date range and a compass direction are not a list",
      jdrender._hyphen_list("The role runs from 2024 - 2025 and covers the East - West "
                            "corridor of the whole region, generally speaking.") is None)
NUM_OK = ("Requirements Bachelors degree or equivalent practical experience in a related "
          "field. 3+ years managing content platforms such as Contentful and WordPress. "
          "2+ years of A/B or multivariate testing experience with third party vendors. "
          "5+ years of hands on analytics work across the whole acquisition funnel.")
check("a number-opening requirements list is recovered",
      len((jdrender._number_list(NUM_OK) or (0, []))[1]) == 3)
check("one stray number in prose is not a list",
      jdrender._number_list("The company was founded in 2019. 5 years later the team had "
                            "grown to four hundred people worldwide and was still hiring.") is None)
REP_OK = ("Understanding of CI/CD concepts and Git Some exposure or working knowledge with "
          "Docker, Kubernetes or other containerization technologies Some exposure or "
          "working knowledge debugging issues ranging from the operating system and the "
          "application all the way to the cloud Some exposure or working knowledge with "
          "building and supporting large-scale production services including logging")
check("a list with NO separator is recovered from its repeated opener",
      len((jdrender._repeat_list(REP_OK) or (0, []))[1]) >= 3)
check("a repeated determiner is not a list",
      jdrender._repeat_list(
          "The company was founded in 2010 and has grown steadily since then. The company now "
          "employs four hundred people across nine offices worldwide today. The company "
          "believes in a flat structure and gives engineers real ownership of the work.") is None,
      "REPEAT_STOP: 'The company' repeats in prose constantly")
check("a recovered list's lead-in becomes its HEADING when it is one",
      ("h", "Basic Qualifications") in jdrender.presplit(
          [("p", "Basic Qualifications 3+ years managing content platforms and public "
                 "websites at scale. 2+ years of A/B or multivariate testing experience "
                 "with third party vendors. 5+ years of hands on analytics work across "
                 "the whole acquisition funnel and beyond.")]),
      "otherwise it renders as a two-word stub AND the section label is thrown away")

print("\nLEGAL: CUT IT OFF THE PARAGRAPH, THEN DECIDE WHETHER TO LIFT IT")
BENEFITS_PLUS_EEO = (
    "What We Offer\n"
    "Medical, dental and vision cover from day one, a 401(k) with a company match, twenty days "
    "of paid time off and a genuine budget for the tools you need. Life insurance and short "
    "and long term disability are included at no cost to the employee. "
    "All qualified applicants will receive consideration for employment without regard to "
    "race, colour, religion, sex, national origin, disability or protected veteran status.")
h = jdrender.render_jd(BENEFITS_PLUS_EEO)
check("the notice is collapsed", "jdlegal" in h)
head, _, tail = h.partition("<details")
check("...and the BENEFITS it was welded to are still on the page",
      "401(k)" in head and "paid time off" in head,
      "lifting the whole node would have hidden a real benefits list -- 71% of uncollapsed "
      "notices are a single node like this one")
check("...and the notice itself is inside the disclosure", "protected veteran" in tail)
# The guard that keeps lift_legal from eating a description.
ALL_LEGAL = ("All qualified applicants will receive consideration without regard to race. "
             "Reasonable accommodations are available on request to applicants with disabilities.")
check("a description that is ONLY notices is not lifted away",
      "jdlegal" not in jdrender.render_jd(ALL_LEGAL),
      "a page rendering as one closed <details> looks broken")
STRAY_IN_LIST = ("Responsibilities\n"
                 + "\n".join("- Build subsystem number %d and review it carefully" % i
                              for i in range(1, 9))
                 + "\n- Company is an Equal Opportunity Employer")
check("a requirements list with ONE stray notice item is not lifted",
      "jdlegal" not in jdrender.render_jd(STRAY_IN_LIST),
      "the majority rule, same as split_boilerplate's")


# ---------------------------------------------------------------------------------------------
print("\nTHE CANONICAL STACK: same sections, same order, every posting")
# ON A SYNTHETIC POSTING WRITTEN IN THE WRONG ORDER, deliberately. Whether the 20 fixtures happen
# to be written benefits-last is a property of the fixture; what is under test is that the engine
# does not care either way.
SCRAMBLED = ("What We Offer\n"
             "- Equity grants and a 401(k)\n- Unlimited paid time off\n\n"
             "Basic Qualifications\n"
             "- Five years of production Python\n- Strong SQL and schema design\n\n"
             "About the Role\n"
             "Build the payments service that moves several billion dollars a year.\n\n"
             "Responsibilities\n"
             "- Own the ledger pipeline end to end\n- Partner with the finance team weekly\n")
scr = jdrender.render_jd(SCRAMBLED)
order = re.findall(r'<h4 class="jdh" data-sec="(\w+)"', scr)
check("sections render in SEC_ORDER whatever order the employer wrote them in",
      order == [k for k in jdrender.SEC_ORDER if k in order], repr(order))
check("Overview is first even though this posting opens with benefits",
      order and order[0] == "summary", repr(order))
check("the employer put benefits first and they render last",
      order and order[-1] == "ben", repr(order))
# THE TWIN OF test_job_page.py's ASSERTION, kept here too because that suite boots Flask and
# this one is the fast gate. Our label names the section; theirs still labels the run.
check("our label and the employer's both survive the move",
      "Required Qualifications" in scr and "Basic Qualifications" in scr
      and "What We Offer" in scr and "About the Role" in scr)
check("the jump strip lists exactly the sections that rendered, in the same order",
      [k for k, _l in jdrender.jump_sections(SCRAMBLED)] == order, repr(order))

print("\nEMPTY SECTIONS DO NOT RENDER, AND DO NOT EXPLAIN THEMSELVES")
bare = jdrender.render_jd("We need somebody who can ship quickly and talk to customers.")
check("a posting with no structure renders one section, not eight",
      bare.count('class="jdh"') <= 1, repr(bare[:120]))
for absent in ("Preferred Qualifications", "Skills & Tools", "Compensation"):
    check("...and says nothing about %r" % absent, jdrender.esc(absent) not in bare)

print("\nTHE INFERENCE TIER MAY NOT MISLABEL")
# THE RULE: a section the employer named is theirs, and inference is switched off underneath it.
# Without this, a majority vote could pull an item out of a list the employer had labelled --
# the one failure that would put our words on their claim.
HEADED = ("Minimum Qualifications\n"
          "- Five years of production Python and strong SQL\n"
          "- Own the ledger pipeline end to end and partner with finance\n"
          "- Lead the design reviews for the payments service\n"
          "- Drive the quarterly roadmap with the product team\n")
hd = jdrender.render_jd(HEADED)
check("inference cannot split a list the employer headed",
      'data-sec="resp"' not in hd, "four of five items read as duties; the heading still wins")
check("...and that section is not marked inferred",
      'data-sec="req"' in hd and "data-inferred" not in hd)
# The opposite direction: no heading at all, so inference is free and SHOULD fire.
UNHEADED = ("We are hiring for the payments team.\n"
            "- Own the ledger pipeline end to end\n"
            "- Partner with the finance team on month end close\n"
            "- Lead design reviews for the payments service\n"
            "- Drive the quarterly roadmap with product\n")
un = jdrender.render_jd(UNHEADED)
check("a list nobody headed IS sectioned by its shape", 'data-sec="resp"' in un)
check("...and is marked as ours, not theirs", 'data-inferred="1"' in un)
check("the opening line stays in Overview", un.index('data-sec="summary"') < un.index('data-sec="resp"'))
# A single requirement-shaped sentence is not a Requirements section.
check("one requirement-shaped line does not open a Requirements section",
      'data-sec="req"' not in jdrender.render_jd(
          "We are hiring for the payments team, which owns the ledger.\n"
          "Experience with Python is useful here but we will teach you."),
      "INFER_SHARE and the paragraph rule are what stop this")
# The two false-positive classes that were live during development, kept as regressions.
check("'diversity, equity and inclusion' is not a compensation section",
      not jdrender._INF_BEN.search("We value diversity, equity and inclusion at every level."))
check("'mental health care' is not a benefit",
      not jdrender._INF_BEN.search("a leading provider of evidence-based mental health care"))
check("a real benefits line still reads as one",
      bool(jdrender._INF_BEN.search("We offer a 401(k), paid time off and health insurance.")))

# A per-shape report, NOT an assertion: how many of each fixture shape produce a real stack. The
# fixture's shape mix is a property of the fixture (see the reasoning further up this file), so
# this is printed to make a regression in the inference tier visible in the run output even when
# nothing fails.
_byshape = {}
for s in samples:
    n = len(jdrender.jd_sections(s["jd"]))
    _byshape.setdefault(s.get("shape") or "?", []).append(n)
print("     sections recovered per fixture shape:")
for _sh in sorted(_byshape):
    _v = _byshape[_sh]
    print("       %-24s %d of %d multi-section  %s"
          % (_sh, sum(1 for x in _v if x > 1), len(_v), _v))


# ---------------------------------------------------------------------------------------------
print("\nHIGHLIGHTING")
# NOT samples[0]: that one is a volunteer soccer coach posting and contains no engineering term
# at all, so asserting against it produced zero marks and looked like a highlighter failure.
one = next(s["jd"] for s in samples if "experience" in s["jd"].lower())
check("no terms is byte-identical to no highlighter",
      jdrender.render_jd(one, have=[], missing=[]) == jdrender.render_jd(one))
hl = jdrender.render_jd(one, have=["experience"], missing=["kubernetes"])
marks = re.findall(r'<mark class="kw-(have|miss)">(.*?)</mark>', hl)
check("marks appear and are correctly classed", marks and all(
    (k == "have") == (v.lower() == "experience") for k, v in marks), "%d marks" % len(marks))
check("a term is marked at most %d times" % jdrender.MAX_HITS_PER_TERM,
      len([1 for k, _v in marks if k == "have"]) <= jdrender.MAX_HITS_PER_TERM)
check("headings are never highlighted",
      not re.search(r"<h4[^>]*>[^<]*<mark", jdrender.render_jd(one, have=["experience"])))

BOUND = [
    ("We use SQL daily.", "sql", True, "plain word"),
    ("We use MySQL daily.", "sql", False, "must not match inside a word"),
    ("Strong in C++.", "c++", True, "plus signs, where \\b would never match"),
    ("Owns the (CI/CD) pipeline.", "ci/cd", True, "slash, inside brackets"),
    ("Uses .NET heavily.", ".net", True, "leading dot"),
    ("Experience with Node.js and more.", "node.js", True, "internal dot"),
    ("Ships Go services daily.", "go", False, "under MIN_TERM_LEN, deliberately skipped"),
    ("Familiar with golang.", "gol", False, "prefix of a longer word"),
    ("Builds with Vue and React.", "vue", True, "exactly MIN_TERM_LEN"),
]
for text, term, want, why in BOUND:
    got = "<mark" in jdrender.render_jd(text, have=[term], sections=False)
    check("%-34r + %-8r" % (text[:32], term), got == want, "%-6s %s" % (got, why))

# Longest-first: without it "project" consumes the front of "project management".
lm = jdrender.render_jd("Strong project management skills.",
                        have=["project management", "project"], sections=False)
check("longest term wins", '<mark class="kw-have">project management</mark>' in lm,
      "leftmost-first alternation would have matched 'project' alone")

# ---------------------------------------------------------------------------------------------
print("\nESCAPING HOLDS UNDER HOSTILE INPUT")
NASTY = ('The Role\n<script>alert(1)</script>\nRequirements:\n'
         '- "><img src=x onerror=alert(1)>\n- Uses R&D budgets & <b>bold</b> python\n'
         '- Ends with a stray < and >')
out = jdrender.render_jd(NASTY, have=["python"], missing=["r&d"])
check("no <script survives", "<script" not in out.lower())
check("no <img survives", "<img" not in out.lower())
# NOT `"onerror" not in out`: the description literally contains that word, so it is correct for
# it to appear as escaped TEXT. What must never happen is it appearing as an ATTRIBUTE, i.e.
# inside a real tag. That is what to assert.
check("no event handler inside any real tag",
      not re.search(r"<[^>]*\son[a-z]+\s*=", out, re.I),
      "the word may appear as text, which is fine; as an attribute it is not")
check("ampersands are escaped", "R&amp;D" in out)
check("the only tags are ours",
      set(re.findall(r"</?([a-z0-9]+)", out)) <= {"p", "ul", "li", "h4", "h5", "mark", "span",
                                                  "details", "summary", "dl", "dt", "dd"},
      repr(sorted(set(re.findall(r"</?([a-z0-9]+)", out)))))
check("a term containing & still marks", 'class="kw-miss">R&amp;D</mark>' in out,
      "escaping runs AFTER the match, so an entity cannot be matched into")

# ---------------------------------------------------------------------------------------------
print("\nMARKDOWN SOURCE IS RENDERED AS MARKUP, NOT PRINTED")
# 2.0% of the 18,087 cached descriptions are stored as Markdown, and we used to put the source on
# the page: "**Job description**" over a row of dashes, bare "**" lines, ----- separators.
MD = ("Location: Dallas\n"
      "\n"
      "**Job description**\n"
      "-------------------\n"
      "\n"
      "Requisition ID: 1725348\n"
      "\n"
      "**Location**\n"
      "------------\n"
      "\n"
      "Dallas, Chicago, Atlanta\n"
      "\n"
      "**\n"
      "\n"
      "## About the job\n"
      "\n"
      "The role you are considering**\n"
      "\n"
      "----------------------------------\n"
      "\n"
      "* Lead **cross-functional** teams\n"
      "* Own the __roadmap__\n")
md_nodes = jdrender.jd_nodes(MD)
md_html = jdrender.render_jd(MD)
heads = [v for k, v in md_nodes if k == "h"]
check("a bold line over a rule becomes a heading", "Job description" in heads, repr(heads))
check("so does the one the screenshot showed", "Location" in heads)
check("an ATX '## Heading' becomes a heading", "About the job" in heads)
check("no asterisk survives anywhere in the output", "*" not in md_html)
check("no underscore emphasis survives", "__" not in md_html)
check("no rule of dashes survives", not re.search(r"-{3,}", md_html))
check("a bare '**' line leaves nothing behind",
      not re.search(r"<p>\s*</p>", md_html), "and no empty paragraph in its place")
check("emphasis inside a bullet is unwrapped, not dropped",
      "cross-functional" in md_html and "roadmap" in md_html)
check("a trailing stray marker is stripped, text kept",
      re.search(r"considering\s*<", unchrome(md_html)) is not None, repr(md_html[-150:]))
# Not asserted: WHICH tag it lands in. Stripping the trailing "**" lets the pre-existing
# is_jd_heading() heuristic see a short, unpunctuated line and call it a heading — which is what
# the emphasis was signalling anyway. Pinning <p> here would freeze an unrelated heuristic.
# The load-bearing one: punctuation may go, words may not.
_proj = lambda s: re.sub(r"[^0-9a-z]+", "", re.sub(r"<[^>]+>", " ", unchrome(s)).lower())
check("every WORD of the markdown source survives",
      _proj(md_html) == _proj(re.sub(r"[*_#-]", " ", MD)),
      "alphanumeric projection, so only markers differ")
# A rule under a long paragraph is a separator, not a title for it.
LONGP = ("x" * 140) + "\n" + ("-" * 20) + "\n"
check("a rule under a LONG paragraph does not promote it to a heading",
      not [v for k, v in jdrender.jd_nodes(LONGP) if k == "h"])
# Backslash escapes. A writer typing "cross-functional" into a Markdown field gets it stored as
# "cross\-functional", which we printed verbatim. Only PUNCTUATION is unescaped: a backslash
# before a letter is a regex or a Windows path (\S, \D and friends are in 24 cached descriptions)
# and losing it would corrupt real text rather than tidy it.
for src, want, why in [
    ("cross\\-functional and end\\-to\\-end", "cross-functional and end-to-end", "hyphens, the case on screen"),
    ("a \\*literal asterisk\\* stays", "a *literal asterisk* stays", "the escaped char itself survives"),
    ("regex \\S and \\D survive", "regex \\S and \\D survive", "backslash before a LETTER is not an escape"),
    ("path C:\\Users\\kunal", "path C:\\Users\\kunal", "nor in a Windows path"),
]:
    check("strip_md(%s)" % repr(src)[:34], jdrender.strip_md(src) == want, why)
check("escapes are gone from rendered output",
      "\\" not in jdrender.render_jd("Own the end\\-to\\-end lifecycle.\n"))

# ---------------------------------------------------------------------------------------------
print("\nA STACKED METADATA HEADER BECOMES A FIELD LIST, NOT A LADDER OF PARAGRAPHS")
# Some boards emit the header one line at a time with a blank between each, so every label and
# every value became its own <p>. Only 12 of 18,087 cached descriptions do this, which is exactly
# why the labels are matched against a VOCABULARY: the values ("None", "Angular", "Software
# Engineering") are shaped just like the labels, so a structural guess would invert the pairs.
KV = ("Clearance Level\n\nNone\n\nCategory\n\nSoftware Engineering\n\n"
      "Location\n\nRemote, Working from the USA\n\n"
      "Key Skills For Success\n\nAmazon Web Services (AWS)\n\nAngular\n\nJava (Programming Language)\n\n"
      "##### **Your Impact**\n\nOwn your opportunity to work alongside federal civilian agencies.\n")
kv_nodes = jdrender.jd_nodes(KV)
kv_html = jdrender.render_jd(KV, have=["java", "aws"])
kinds = [k for k, _v in kv_nodes]
fields = dict(v for k, v in kv_nodes if k == "kv")
check("each label is paired with its value", fields.get("Clearance Level") == ["None"], repr(fields)[:90])
check("and so is the next one", fields.get("Category") == ["Software Engineering"])
check("a label with SEVERAL values keeps all of them",
      fields.get("Key Skills For Success") ==
      ["Amazon Web Services (AWS)", "Angular", "Java (Programming Language)"], repr(fields.get("Key Skills For Success")))
check("the header does not leak into the description below",
      kinds[-2:] == ["h", "p"], repr(kinds))
check("consecutive fields render as ONE definition list", kv_html.count("<dl") == 1, repr(kv_html[:60]))
check("values are highlighted — they are the skills the reader is scanning for",
      '<mark class="kw-have">AWS</mark>' in kv_html and '<mark class="kw-have">Java</mark>' in kv_html)
check("no field text is lost",
      re.sub(r"[^0-9a-z]+", "", re.sub(r"<[^>]+>", " ", unchrome(kv_html)).lower()) ==
      re.sub(r"[^0-9a-z]+", "", re.sub(r"[*#\\]", " ", KV).lower()),
      "alphanumeric projection")
# The two ways this could misfire, both checked rather than assumed.
UNKNOWN = "Favourite Colour\n\nBlue\n\nSecond Thing\n\nGreen\n"
check("an UNKNOWN label is left exactly as it renders today",
      "<dl" not in jdrender.render_jd(UNKNOWN), "vocabulary only, never a shape guess")
LATE = ("We are hiring a developer for our platform team and this is a real sentence.\n\n"
        "Location\n\nBoston\n")
check("a field name appearing AFTER the description starts is not pulled out of context",
      "<dl" not in jdrender.render_jd(LATE), "the header parser stops once prose begins")
# Some boards mark a FIELD up as a heading: "##### **REQ#:****RQ225292**".
ASHEAD = ("Clearance Level\n\nNone\n\n##### **REQ#:****RQ225292**\n\n"
          "##### **Public Trust:****Other**\n\n##### **Your Impact**\n\n"
          "Own your opportunity to work with federal agencies.\n\nWhy Join Us: The Team\n\nWe build things.\n")
ah = dict(v for k, v in jdrender.jd_nodes(ASHEAD) if k == "kv")
ah_kinds = [k for k, _v in jdrender.jd_nodes(ASHEAD)]
check("a field dressed up as a heading joins the field list", ah.get("REQ#") == ["RQ225292"], repr(ah))
check("...and so does the next one", ah.get("Public Trust") == ["Other"])
check("a REAL heading is still a heading", "Your Impact" in
      [v for k, v in jdrender.jd_nodes(ASHEAD) if k == "h"])
check("a heading that merely CONTAINS a colon is left alone",
      "Why Join Us: The Team" in [v for k, v in jdrender.jd_nodes(ASHEAD) if k == "h"],
      "not every 'x: y' is a field — only a known field name is")

# A flattened Abbott-style posting puts the real role after its benefits preamble.
# Both headings must reset custody, or the summary and duties inherit Compensation.
OPPORTUNITY = ("Benefits Employees receive tuition reimbursement and medical coverage. "
               "THE OPPORTUNITY This engineer will build secure services for customers. "
               "What You'll Work On Design reliable APIs and maintain deployment pipelines. "
               "Required Qualifications 7 years of engineering experience.")
opp_sections = {key: " ".join(head + " " + " ".join(jdrender._node_text(n) for n in nodes)
                             for head, nodes, _inferred in groups)
                for key, groups in jdrender.jd_sections(OPPORTUNITY)}
check("an uppercase opportunity heading resets the benefits section",
      "build secure services" in opp_sections.get("summary", ""), repr(opp_sections))
check("What You'll Work On labels responsibilities",
      "Design reliable APIs" in opp_sections.get("resp", ""))
check("duties and opportunity do not stay in compensation",
      "engineer" not in opp_sections.get("ben", "") and "APIs" not in opp_sections.get("ben", ""))
check("opportunity regrouping preserves every word",
      sorted(re.findall(r"[a-z0-9]+", " ".join(opp_sections.values()).lower())) ==
      sorted(re.findall(r"[a-z0-9]+", OPPORTUNITY.lower())))

print()
if FAILS:
    print("FAILURES (%d):" % len(FAILS))
    for f in FAILS:
        print("   %s" % f)
    raise SystemExit(1)
print("ALL JD RENDER CHECKS PASS")
