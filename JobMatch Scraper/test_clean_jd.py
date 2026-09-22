#!/usr/bin/env python3
"""core.clean_jd: the one door every reader of a stored description goes through.

WHY THIS SUITE EXISTS. fetch_jd's last resort stores the WHOLE PAGE when no selector finds a
description, and the gate on it was one-sided -- MIN_PAGE_JD_CHARS is a FLOOR, so a shell that
was too LONG was invisible to it. 1,052 of careers.google.com's 1,099 cached rows were the
site's navigation bar stored as a job description, 790 of them truncated at exactly 8,000
characters, and nothing downstream could see it: over 400 characters means never "thin", never
thin means never retried, and _accept_jd's 3x gain rule then made it permanent.

The checks are in two directions and the SECOND one is the one that matters. Removing
navigation is easy; removing it without taking the posting with it is not. The first version of
the cut jumped to the next section heading and threw away real text on 37% of the rows it
touched, because Amazon's nav is one marker at character 98 and its next heading is 1,238
characters later, with the whole role summary in between.

    python test_clean_jd.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core

FAILS = []


def check(name, cond, extra=""):
    if not cond:
        FAILS.append(name)
    print("  %s %-62s %s" % ("ok " if cond else "FAIL", name, extra))


# A real description, long enough to clear _MIN_JD_CHARS on its own.
BODY = ("About the job We are hiring a payments engineer to build and operate the settlement "
        "service that moves money between our merchants and their banks. You will own the "
        "pipeline end to end. Minimum qualifications: Bachelor's degree in a technical field. "
        "5 years of experience in program management. Preferred qualifications: experience "
        "with distributed systems and a track record of shipping. Benefits: 401(k) with "
        "company match and 20 days of vacation per year, accruing each pay period.")

# careers.google.com, reproduced in shape from the cached corpus: icon ligature names, then the
# results list, then the posting. The listing rows carry no furniture WORDS at all, which is why
# the cut cannot stop at the last icon name.
GOOGLE = ("Technical Program Manager III, Security, Core - Google Careers Careers Careers home "
          "Home work_outline Jobs expand_more noogler_hat Students expand_more google How we "
          "work expand_more handyman How we hire expand_more person_outline Your career "
          "expand_more job details arrow_back Back to jobs search Jobs search results 3,311 "
          "jobs matched "
          + "Software Engineer Mountain View, CA, USA ; Austin, TX, USA ; +2 more " * 12
          + BODY)

# amazon.jobs: ONE furniture marker near the front, then a menu with no markers in it, then the
# posting. The menu is why "cut at the last icon name" is not enough either.
AMAZON = ("Program Manager - Job ID: 10455889 | Amazon.jobs Skip to main content Home Teams "
          "Locations Job categories My career My applications My profile Account security "
          "Settings Sign out Resources Accommodations Benefits How We Hire Leadership "
          "principles Working at Amazon FAQ Program Manager, Relo Ops Job ID: 10455889 | "
          "Amazon.com Services LLC Description " + BODY)


print("=" * 94)
print("core.clean_jd")
print("=" * 94)

print("\nA CAPTURED CAREERS PAGE LOSES ITS FURNITURE AND KEEPS ITS POSTING")
g, gv = core.clean_jd(GOOGLE)
check("the verdict names what happened", gv == "chrome-stripped", gv)
check("no icon ligature survives", "work_outline" not in g and "expand_more" not in g)
check("no results list survives", "jobs matched" not in g and "+2 more" not in g)
check("it opens on the posting's own heading", g.lower().startswith("about the job"), g[:40])
check("the qualifications are still there", "5 years of experience" in g)
check("the benefits are still there", "20 days of vacation" in g)

a, av = core.clean_jd(AMAZON)
check("a menu with no icons in it is furniture too", av == "chrome-stripped", av)
check("...and the menu is gone", "Job categories" not in a and "Sign out" not in a)
# THE EXPENSIVE DIRECTION. Amazon's first heading is 1,200 characters past its only furniture
# marker, so a rule that cuts to the next heading eats the entire role summary in between.
check("the role summary between the menu and the first heading SURVIVES",
      "settlement service that moves money" in a,
      "this is the 37%-of-rows regression the first cut rule caused")

print("\nA CLEAN DESCRIPTION IS RETURNED UNTOUCHED")
# Google internship pages put real application instructions immediately after their
# share controls. "the role" in the final sentence is prose, not a section heading.
APPLICATION = (
    "Please complete your application before October 9, 2026.\n"
    "Applications will be reviewed on a rolling basis and candidates should apply early.\n"
    "Timing on when you can hear back can take upwards of 90 days. "
    "If you have not heard from us, we likely proceeded with other candidates for the role.\n"
    "Participation requires that you are located in the United States during the internship.\n"
)
for name, separator in (("multiline", "\n"), ("flattened", " ")):
    application = APPLICATION if separator == "\n" else APPLICATION.replace("\n", " ")
    posting = "Google Intern\nCopy link\nEmail a friend" + separator + application + BODY
    cleaned, verdict = core.clean_jd(posting)
    check("%s application deadline survives navigation cleaning" % name,
          cleaned.startswith("Please complete your application before October 9, 2026."), cleaned[:90])
    check("%s application instructions remain complete" % name,
          application.strip() in cleaned and "90 days" in cleaned)
    check("%s Google posting stays readable and idempotent" % name,
          verdict == "chrome-stripped" and core.clean_jd(cleaned) == (cleaned, "ok"))
# A real heading can follow introductory prose too; proximity alone must not erase it.
intro = "Applications close September 30. The team works on payment systems. "
cleaned, _ = core.clean_jd("Email a friend " + intro + BODY)
check("a nearby genuine heading does not erase preceding application prose",
      cleaned.startswith(intro) and BODY in cleaned)

c, cv = core.clean_jd(BODY)
check("verdict is ok", cv == "ok", cv)
check("not one character is removed", c == BODY.strip())
# A posting may legitimately say a furniture-ish phrase once, at the end. That is not a page.
tail = BODY + " Share this job with a friend."
t, tv = core.clean_jd(tail)
check("a single furniture phrase in the body does not condemn the posting", tv != "not-a-posting",
      tv)
check("...and nothing is cut off the front", t.startswith("About the job"))

print("\nA PAGE THAT IS NOT A POSTING SAYS SO")
check("a dead-link shell", core.clean_jd(
    "We're sorry, this link is no longer active. " + "Please search again. " * 40
)[1] == "not-a-posting")
check("a Salesforce loading shell", core.clean_jd(
    "Sorry to interrupt. CSS Error Refresh. " * 30)[1] == "not-a-posting")
check("an expired session wrapped in a cookie notice", core.clean_jd(
    "Necessary cookies help make a website usable. Your session has expired. " * 20
)[1] == "not-a-posting")
check("furniture the cut could not clear", core.clean_jd(
    "work_outline Jobs expand_more arrow_back Back to jobs search " * 30)[1] == "not-a-posting")
check("empty text", core.clean_jd("")[1] == "not-a-posting")
check("None", core.clean_jd(None) == ("", "not-a-posting"))

print("\nLENGTH IS NOT JUDGED HERE")
# _MIN_JD_CHARS, _is_thin_jd and the per-host retry ledger own "too short to score", and a
# short description that went down the junk path would lose its repair route.
short = "Join our team. We are hiring."
check("a short description is not called junk", core.clean_jd(short)[1] == "ok",
      "thinness belongs to _is_thin_jd, not to this")
check("...and it is returned intact", core.clean_jd(short)[0] == short)

print("\nTHE PREFILTER AND THE PATTERNS AGREE")
# Every literal exists to let a pattern run. A literal that no pattern can match is a full scan
# bought for nothing; a pattern whose literal is missing NEVER RUNS AT ALL, silently.
# ONE CONCRETE EXAMPLE PER ALTERNATIVE, checked against BOTH halves. Deriving the examples from
# the pattern source by stripping regex syntax was the first version of this and it tested the
# stripping, not the patterns. A written-out list is also the readable inventory of what this
# module considers furniture, which the regex source is not.
FURNITURE = (
    "work_outline", "expand_more", "expand_less", "arrow_back", "arrow_forward",
    "corporate_fare", "info_outline", "navigate_next", "navigate_before", "person_outline",
    "noogler_hat", "handyman", "bar_chart", "keyboard_arrow_down", "open_in_new", "more_vert",
    "3,311 jobs matched", "1 job matched", "Jobs search results", "Go to next page",
    "Back to search", "Back to jobs search", "Return to search results",
    "Skip to main content", "Skip to content", "Share this job", "Save this job",
    "Print this job", "Email a friend", "View all jobs", "Job categories", "Job alerts",
    "My applications", "My profile", "My career", "Sign out",
    # Both numbers, deliberately: the pattern allows "cookie" and "cookies" on every noun, and
    # the first run of the check below found the plural half unreachable from the prefilter.
    "Cookie policy", "Cookies policy", "Cookie settings", "Cookies settings",
    "Cookie preferences", "Cookies preferences", "Accept all cookies", "We use cookies",
    "You need to enable JavaScript", "JavaScript is disabled", "JavaScript is required",
)
misses = []
for probe in FURNITURE:
    low = probe.lower()
    if not core._CHROME_RX.search(low):
        misses.append("%s: no PATTERN matches it" % probe)
    elif not any(lit in low for lit in core._CHROME_LITERALS):
        # The expensive one: the pattern is correct and the prefilter never lets it run.
        misses.append("%s: pattern matches but no PREFILTER literal does" % probe)
check("every furniture phrase clears both the prefilter and the pattern", not misses,
      "; ".join(misses[:3]))
# ...and the other direction: a literal no pattern can act on buys a full scan for nothing.
unused = [lit for lit in core._CHROME_LITERALS
          if not any(lit in p.lower() and core._CHROME_RX.search(p.lower())
                     for p in FURNITURE)]
check("every prefilter literal is earned by a real phrase", not unused, unused or "")

print("\nTHE RESULTS-LIST RULE IS COUNTED, NOT MATCHED")
# A genuine posting names one office this way. Measured over 4,000 clean descriptions, 0.8%
# contain the pattern once and NONE contain it three times.
one_office = BODY + " This role is based in Austin, TX, USA."
check("one office mentioned in prose is not a results list",
      core.clean_jd(one_office)[1] == "ok", core.clean_jd(one_office)[1])
check("three of them in a row is", len(core._LISTING_RX.findall(
    "Eng Austin, TX, USA ; Eng Seattle, WA, USA ; Eng Boston, MA, USA")) >= core._LISTING_MIN)
check("one of them is below the threshold",
      len(core._LISTING_RX.findall(one_office)) < core._LISTING_MIN)

print("\nMARKDOWN ESCAPES, WHICH THE RENDERER HAS ALWAYS STRIPPED AND THE PARSER NEVER DID")
# The Applied Materials report: the SAME posting stored twice, from its Workday host as plain
# text and from the employer's own host as markdown. One read "5+ years" and answered 5; the
# other held "5\+ years", and _EXP_YEARS_RE needs the plus or the space right after the digit,
# so it matched nothing and the posting claimed to state no requirement at all. Measured:
# 2,327 of 42,419 descriptions (5.5%) carry an escape and 820 gain a year floor without it.
_ESCAPED = ("* 5\\+ years of Project or Program Management experience in enterprise IT "
            "environments.\n* 2\\+ years managing endpoint initiatives.\n") + BODY
check("an escaped plus no longer hides the requirement",
      core.experience_years(core.clean_jd(_ESCAPED)[0]) == 5,
      core.experience_years(core.clean_jd(_ESCAPED)[0]))
check("the raw text really was unreadable before it",
      core.experience_years(_ESCAPED.split("\n")[0]) is None,
      "if this ever passes, the escape is being handled somewhere else and this case is moot")
check("cross\\-functional loses its backslash", "\\" not in core.clean_jd(
    "We need cross\\-functional delivery. " + BODY)[0])
# PUNCTUATION ONLY. A backslash before a letter is a Windows path or a regex, and \S / \D / \b
# appear in 24 of the cached descriptions.
_KEEP = "Path C:\\Users\\kunal and the regex \\S and \\D survive. " + BODY
check("a backslash before a LETTER survives untouched",
      "C:\\Users\\kunal" in core.clean_jd(_KEEP)[0] and "\\S" in core.clean_jd(_KEEP)[0])
# ONE RULE, TWO DEFINITIONS, because jdrender imports the standard library and nothing else on
# purpose and core therefore cannot borrow it. This is the guard that stops them drifting.
import jdrender                                                          # noqa: E402
check("core._MD_ESCAPE and jdrender.MD_ESCAPE are the same pattern",
      core._MD_ESCAPE.pattern == jdrender.MD_ESCAPE.pattern,
      "%r vs %r" % (core._MD_ESCAPE.pattern, jdrender.MD_ESCAPE.pattern))

print("\nIDEMPOTENT: cleaning a cleaned description changes nothing")
for name, src in (("google", GOOGLE), ("amazon", AMAZON), ("clean", BODY)):
    once = core.clean_jd(src)[0]
    check("%s survives a second pass unchanged" % name, core.clean_jd(once)[0] == once)

print("\nANALYZE_JD CARRIES THE VERDICT")
# not-a-posting reads as THIN, which is the answer the whole pipeline already knows how to
# carry: score withheld, "JD pending" on the card, _row_pending in the feed.
junk = core.analyze_jd("work_outline Jobs expand_more arrow_back Back to jobs search " * 30)
check("a page scores as thin, not as a job", junk["thin"] is True)
check("...and says why", junk["verdict"] == "not-a-posting", junk["verdict"])
real = core.analyze_jd(BODY * 3)
check("a real description does not", real["thin"] is False and real["verdict"] == "ok",
      "%s / %s" % (real["thin"], real["verdict"]))

print()
if FAILS:
    print("FAILURES (%d):" % len(FAILS))
    for f in FAILS:
        print("   " + f)
    sys.exit(1)
print("ALL CLEAN_JD CHECKS PASS.")
