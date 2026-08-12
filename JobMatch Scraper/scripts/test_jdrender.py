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


def text_of(html):
    """Visible text: drop tags, then un-escape the four entities esc() writes.

    The <summary> label is dropped WITH its contents, because "Legal and Equal Opportunity
    Notices" is chrome this renderer adds rather than anything the employer wrote. Leaving it in
    made the projection 31 characters longer than the input and read as text loss inverted.
    """
    t = re.sub(r"<summary>.*?</summary>", " ", html, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return (t.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&amp;", "&"))


print("\nNO TEXT IS DROPPED, on any path (alphanumeric projection)")
for label, kw in (("plain", {}), ("sectioned + highlighted",
                                 {"have": ["python", "sql", "aws"], "missing": ["kubernetes"]})):
    bad = []
    for s in samples:
        html = jdrender.render_jd(s["jd"], sections=(label != "plain"), **kw)
        if alnum(text_of(html)) != alnum(s["jd"]):
            bad.append(s.get("title") or "?")
    check("all %d samples keep every character (%s)" % (len(samples), label), not bad,
          "" if not bad else "lost text in %d: %s" % (len(bad), bad[:2]))

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
      set(re.findall(r"</?([a-z0-9]+)", out)) <= {"p", "ul", "li", "h4", "mark", "details",
                                                  "summary"},
      repr(sorted(set(re.findall(r"</?([a-z0-9]+)", out)))))
check("a term containing & still marks", 'class="kw-miss">R&amp;D</mark>' in out,
      "escaping runs AFTER the match, so an entity cannot be matched into")

print()
if FAILS:
    print("FAILURES (%d):" % len(FAILS))
    for f in FAILS:
        print("   %s" % f)
    raise SystemExit(1)
print("ALL JD RENDER CHECKS PASS")
