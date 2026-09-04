#!/usr/bin/env python3
"""Does the PARSER read what the READER can see? Every stored description, both directions.

WHY THIS EXISTS. Two reading failures in this corpus were found by the owner opening one job
and noticing the page contradicted itself, and both turned out to be whole CLASSES rather than
one-offs: a careers-site navigation bar stored as a description (1,052 rows, all of Google), and
markdown backslash escapes that jdrender strips before drawing and core.experience_years never
did, so "5\\+ years of Project or Program Management experience" read as no requirement at all
(820 rows, mostly Microsoft). Finding those one screenshot at a time does not scale, and neither
does trusting a parser because its unit tests pass.

THE METHOD, and its one honest limitation. This does NOT re-implement the parser and diff the
two -- that only ever proves the copy agrees with itself. It asks a much narrower question with
a deliberately CONSERVATIVE pattern: does the text a reader sees contain a phrase that any human
would call a stated requirement? "5+ years of experience", "minimum of three years' experience".
Where that is present and core.experience_years answers None, the parser has missed something a
person would not. Where the parser answers a number and no such phrase exists anywhere, it may
have invented one.

The limitation: the conservative pattern finds a floor on far fewer postings than the parser
does, by design -- it is a tripwire, not a second parser. A LOW miss count means the parser
reads everything obvious. It cannot prove the parser reads everything subtle.

GROUPED BY HOST, because that is what turns a list of rows into a bug. Both failures above were
invisible as scattered rows and obvious the moment they were grouped: one host, one cause.

    python scripts/audit_jd_reading.py                 # the whole cache
    python scripts/audit_jd_reading.py --sample 5000   # a fixed-seed subset
    python scripts/audit_jd_reading.py --show 8        # print more examples per class
    python scripts/audit_jd_reading.py --check         # exit 1 if the miss rate regresses

Reads jd_cache.json.gz only — no database, no network. NOT a CI suite and deliberately not in
run_tests.py's SUITES: that cache is 48 MB and gitignored, so a CI runner has no input for it and
the script would either fail or, worse, pass by finding nothing. It is a bench tool you run here
against the real corpus. scripts/measure_jd_reading.py is the one with the CI-shaped gate.

RESULT WHEN IT WAS WRITTEN, 2026-09-03, over all 42,419 cached descriptions: 41,254 readable,
27,058 where the parser and a conservative reader agree, and ONE miss — an employer who typed
"2-3 yearsof experience" without the space. Two parser gaps it found on the way to that number
are fixed in core: markdown backslash escapes (820 rows) and the compound-adjective form
"1-year experience" (846 rows use the hyphen).
"""
import argparse
import gzip
import json
import os
import random
import re
import sys
from collections import Counter
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core                                                              # noqa: E402

CACHE = "jd_cache.json.gz"

# WHAT A HUMAN WOULD CALL A STATED REQUIREMENT, and nothing looser. Every alternative here binds
# a number to the word "experience" or to an explicit minimum, within one clause, so that a
# vesting schedule, a company's age and a contract length cannot satisfy it. This is the
# tripwire the parser is measured against; if it grows liberal it stops being evidence.
#
# (?<!\d) AND (?!\d) ARE NOT DECORATION. Without them \d{1,2} happily matches the first two
# digits of a three-digit number, and the first run of this audit reported GE Vernova's "With
# over 130 years of experience" as a thirteen-year requirement the parser had missed. Two of the
# four misses it found were this, i.e. the audit was wrong about half the time it complained.
GROUND_TRUTH = re.compile(
    r"(?<!\d)(\d{1,2})(?!\d)\s*\+?\s*(?:-|to)?\s*(?:\d{1,2}(?!\d))?\s*years?['’]?\s*"
    r"(?:of\s+)?(?:relevant\s+|professional\s+|progressive\s+|related\s+|prior\s+|"
    r"industry\s+|hands[- ]on\s+)?experience\b"
    r"|\bminimum\s+(?:of\s+)?(?<!\d)(\d{1,2})(?!\d)\s*\+?\s*years?\b"
    r"|\bat\s+least\s+(?<!\d)(\d{1,2})(?!\d)\s*\+?\s*years?\b", re.I)

# The phrases those alternatives must NOT be counting. Checked in the same clause as the hit --
# a posting may perfectly well mention a 12-month contract elsewhere and still state a floor.
NOT_A_REQUIREMENT = re.compile(
    r"vest|401|equity|tenure|anniversar|founded|has been|have been|celebrat|"
    r"over \d+ years|more than \d+ years|drawing on|our history", re.I)


def clause_of(text, at):
    """The clause a match sits in, so the exclusions above are judged locally."""
    lo = max((text.rfind(c, 0, at) for c in ".;\n•"), default=-1)
    hi = min((p for p in (text.find(c, at) for c in ".;\n•") if p != -1),
             default=len(text))
    return text[lo + 1:hi]


def ground_truth_floor(text):
    """The lowest number a conservative reading finds, or None. Deliberately not a maximum:
    this is asking 'would a person see a requirement here', not 'which one governs'."""
    found = []
    for m in GROUND_TRUTH.finditer(text):
        if NOT_A_REQUIREMENT.search(clause_of(text, m.start())):
            continue
        n = next((int(g) for g in m.groups() if g), None)
        if n is not None and n <= 20:
            found.append(n)
    return min(found) if found else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="0 = every cached description")
    ap.add_argument("--seed", type=int, default=7, help="fixed so runs stay comparable")
    ap.add_argument("--show", type=int, default=4, help="examples printed per class")
    ap.add_argument("--check", action="store_true", help="exit 1 if a threshold is breached")
    ap.add_argument("--max-miss", type=float, default=2.0,
                    help="%% of descriptions the parser may miss, for --check")
    a = ap.parse_args()

    if not os.path.exists(CACHE):
        sys.exit("no %s here — run from the app directory." % CACHE)
    jds = json.load(gzip.open(CACHE, "rt", encoding="utf-8"))
    urls = [u for u, t in jds.items() if isinstance(t, str) and t.strip()]
    if a.sample and a.sample < len(urls):
        random.seed(a.seed)
        urls = random.sample(urls, a.sample)

    n = agree = miss = extra = 0
    miss_host, extra_host, verdicts = Counter(), Counter(), Counter()
    miss_eg, extra_eg = [], []

    for u in urls:
        # THE TEXT THE READER SEES, which is the whole point — clean_jd is the one door and
        # auditing the raw column would measure a string nothing renders.
        text, verdict = core.clean_jd(jds[u])
        verdicts[verdict] += 1
        if verdict == "not-a-posting" or len(text.strip()) < core._MIN_JD_CHARS:
            continue                              # a page or a stub; the reader is told so
        n += 1
        theirs = ground_truth_floor(text)
        ours = core.experience_years(text)
        host = urlsplit(u).netloc.lower()
        if theirs is not None and ours is None:
            miss += 1
            miss_host[host] += 1
            if len(miss_eg) < a.show:
                m = GROUND_TRUTH.search(text)
                miss_eg.append((u, theirs, text[max(0, m.start() - 70):m.end() + 40]))
        elif theirs is None and ours is not None:
            extra += 1
            extra_host[host] += 1
            if len(extra_eg) < a.show:
                extra_eg.append((u, ours, ""))
        else:
            agree += 1

    def pct(x):
        return 100.0 * x / n if n else 0.0

    print("=" * 92)
    print("JD READING AUDIT — the parser against a conservative reading of the same text")
    print("=" * 92)
    print("cached descriptions      : %d" % len(urls))
    for k in ("ok", "chrome-stripped", "not-a-posting"):
        print("   %-18s %6d" % (k, verdicts[k]))
    print("judged (readable, over %d chars): %d" % (core._MIN_JD_CHARS, n))
    print()
    print("  agree                  : %6d  (%5.1f%%)" % (agree, pct(agree)))
    print("  PARSER MISSED IT       : %6d  (%5.1f%%)   <- the number that matters" % (miss, pct(miss)))
    print("  parser found more      : %6d  (%5.1f%%)   (not a defect on its own —" % (extra, pct(extra)))
    print("                                            the tripwire is deliberately narrow)")

    if miss_host:
        print("\nMISSES BY HOST — one host dominating is a CLASS, not a scattering:")
        for h, c in miss_host.most_common(12):
            print("   %-44s %5d" % (h[:44], c))
    for u, want, ctx in miss_eg:
        print("\n   %s" % u[:86])
        print("      a reader sees %s here: %r" % (want, re.sub(r"\s+", " ", ctx)[:150]))

    if a.check:
        if pct(miss) > a.max_miss:
            print("\nFAILED: parser misses %.1f%% of plainly-stated requirements (max %.1f)"
                  % (pct(miss), a.max_miss))
            return 1
        print("\nOK: misses %.1f%% <= %.1f" % (pct(miss), a.max_miss))
    return 0


if __name__ == "__main__":
    sys.exit(main())
