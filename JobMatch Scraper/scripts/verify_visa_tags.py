#!/usr/bin/env python3
"""Sanity-check visa_tags.json against the live corpus.

The index is built by fuzzy-matching our company names onto federal filing names, so the
thing that can go wrong isn't a crash — it's a plausible-looking wrong answer. This prints
named assertions plus the coverage numbers, and lists the biggest untagged employers so a
systematic matching failure shows up as a familiar name near the top.

    python scripts/verify_visa_tags.py
"""
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db

FAILS = []


def check(label, cond, detail=""):
    print("  %-58s %s%s" % (label, "ok" if cond else "FAIL", ("  " + detail) if detail else ""))
    if not cond:
        FAILS.append(label)


def main():
    idx = core.load_visa_tags()
    print("index employers: %d\n" % len(idx))
    check("index loaded", len(idx) > 1000, "%d keys" % len(idx))

    # Expectations are what FY2026 Q2 actually contains, not what we assume about the
    # employer. Google has no certified PERM filing that quarter beyond "google public
    # sector" (a single case, below the prefix-corroboration floor), so asserting
    # green_card for Google would be asserting a bug into existence.
    print("\nspot checks:")
    for name, must in [("Amazon", {"h1b", "green_card"}),
                       ("Google", {"h1b"}),
                       ("JPMorgan Chase", {"h1b", "green_card"}),
                       ("Deloitte", {"h1b"}),
                       ("Microsoft", {"h1b", "green_card"}),
                       ("Oracle", {"h1b", "green_card"}),
                       ("Boeing", {"h1b"})]:
        got = set(core.visa_tags(name, idx))
        check("%-22s has %s" % (name, ",".join(sorted(must))), must <= got,
              "got %s" % (",".join(sorted(got)) or "none"))

    check("nonsense name gets nothing",
          core.visa_tags("Zzqx Nonexistent Holdings", idx) == ())
    check("empty name gets nothing", core.visa_tags("", idx) == ())
    check("tags come back in canonical order",
          all(list(core.visa_tags(c, idx)) ==
              [t for t in core.VISA_TAGS if t in core.visa_tags(c, idx)]
              for c in ("Amazon", "Google", "Deloitte")))

    rows = db.load_jobs()
    comps = collections.Counter((r.get("company") or "").strip() for r in rows)
    comps.pop("", None)
    per_co, per_job = collections.Counter(), collections.Counter()
    tagged_co = tagged_job = 0
    for c, n in comps.items():
        t = core.visa_tags(c, idx)
        if t:
            tagged_co += 1
            tagged_job += n
        for k in t:
            per_co[k] += 1
            per_job[k] += n

    print("\ncoverage over %d jobs / %d companies:" % (len(rows), len(comps)))
    print("  %-12s %9s %9s" % ("tag", "companies", "jobs"))
    for k in core.VISA_TAGS:
        print("  %-12s %6d    %6d (%2.0f%%)"
              % (k, per_co[k], per_job[k], 100.0 * per_job[k] / max(1, len(rows))))
    print("  %-12s %6d    %6d (%2.0f%%)"
          % ("ANY", tagged_co, tagged_job, 100.0 * tagged_job / max(1, len(rows))))

    check("at least 70%% of jobs carry a tag", tagged_job >= 0.70 * len(rows))
    check("every tag fires on someone", all(per_co[k] for k in core.VISA_TAGS),
          ",".join(k for k in core.VISA_TAGS if not per_co[k]) or "")

    print("\nbiggest UNTAGGED employers (a familiar name here means matching missed it):")
    for c, n in sorted(((c, n) for c, n in comps.items() if not core.visa_tags(c, idx)),
                       key=lambda kv: -kv[1])[:12]:
        print("   %-42s %5d jobs" % (c[:42], n))

    print("\n%s" % ("ALL CHECKS PASSED" if not FAILS else "FAILED: " + "; ".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
