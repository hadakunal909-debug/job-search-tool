#!/usr/bin/env python3
"""Score candidate widenings of the title filter against a real dump of scraped titles.

Feed it a TSV from scripts/dump_titles.py and it answers the only question that matters when
adding to INCLUDE: how many postings does this candidate ADMIT THAT NOTHING ELSE ALREADY DOES,
and are they on target?

    python scripts/dump_titles.py --all --out titles.tsv
    python scripts/score_title_candidates.py --dump titles.tsv --add "programme manager,pmo lead"
    python scripts/score_title_candidates.py --dump titles.tsv --variant plurals
    python scripts/score_title_candidates.py --dump titles.tsv --variant separators

RANKED GREEDILY BY MARGINAL CONTRIBUTION, not by raw hits. This is the method the Amazon keyword
audit established: a dozen PM synonyms all return the same jobs, so raw counts put every one of
them at the top and flatter all of them. A term is worth having only for the rows it is the SOLE
reason for keeping.

WHAT THIS CAN AND CANNOT TELL YOU. For a REMOVAL, prune_offtarget.py can weigh rows against the
feed's match floor, because those rows are already stored and already scored. For an ADDITION
there is no score to read -- the posting was never stored, so no JD was ever fetched. So the
metric here is volume plus composition: a candidate whose admits are one employer repeated a
hundred times is the shape "operations associate" had (Sephora 112, CubeSmart 111) and "trainee"
had (Cintas 125), and both turned out to be noise. Read the samples; counts alone will not say.

EXCLUDE STILL VETOES. Every count below is post-exclude, exactly as title_verdict applies it, so
a candidate can never appear to rescue a title the exclude list turns away.
"""
import argparse
import collections
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scraper

# The "thing" words whose plurals employers actually write ("Programs Manager", "Projects
# Coordinator"). Deliberately not every noun in INCLUDE: _make_matcher's word boundaries exist
# so "program" cannot match "programmer", and a blanket suffix wildcard would undo that.
PLURAL_STEMS = ("project", "program", "product", "operation", "system", "application",
                "solution", "portfolio", "initiative")

# Separator-flexible. Employers write "Project/Program Manager", "Project-Manager", and
# occasionally two spaces; re.escape demands exactly one literal space.
SEP_CLASS = r"[\s\-/&]+"


def load(path, us_only=True):
    rows = []
    with io.open(path, encoding="utf-8", newline="") as fh:
        head = fh.readline().rstrip("\n").split("\t")
        ix = {k: i for i, k in enumerate(head)}
        for line in fh:
            f = line.rstrip("\n").split("\t")
            if len(f) < len(head):
                continue
            if us_only and f[ix["us"]] != "y":
                continue
            rows.append((f[ix["title"]], f[ix["company"]], f[ix["ats"]]))
    return rows


def base_terms():
    """INCLUDE as main() actually runs it -- the base list PLUS resume_terms().

    Not cosmetic: prune_offtarget's docstring records that replaying with the base list alone
    blamed a keyword change for 924 rows when the truth was 450.
    """
    return tuple(scraper.INCLUDE) + tuple(scraper.resume_terms())


def matcher_for(terms, plurals=False, separators=False):
    """A matcher over `terms`, optionally with the two structural relaxations applied.

    Built WORD BY WORD rather than by regex-substituting an already-escaped phrase. The escaped
    form is the wrong thing to edit: it is a pattern, so a stray substitution lands inside an
    escape sequence, and "programmer" must not pick up the plural of "program".

    With both flags off this reproduces scraper._make_matcher exactly -- asserted in
    check_baseline() below, because a baseline that quietly disagrees with the live filter would
    misattribute every gain measured against it.
    """
    # re.escape() escapes the space too, so the joiner has to be escaped as well or the
    # patterns differ textually while behaving identically -- and check_baseline compares text.
    joiner = SEP_CLASS if separators else re.escape(" ")
    parts = []
    for t in terms:
        chunks = []
        for w in t.split(" "):
            # Accept the word as written AND its other number: INCLUDE holds both "program
            # manager" and "operations manager", so the singular stem has to be recovered from a
            # plural entry as well as the other way round.
            base = w[:-1] if (w.endswith("s") and w[:-1] in PLURAL_STEMS) else w
            chunks.append(re.escape(base) + "s?" if (plurals and base in PLURAL_STEMS)
                          else re.escape(w))
        parts.append(joiner.join(chunks))
    # Longest ESCAPED alternative first, matching _make_matcher: it sorts the escaped strings,
    # not the raw phrases, and escaping changes their lengths.
    parts.sort(key=len, reverse=True)
    return re.compile(r"\b(?:%s)\b" % "|".join(parts), re.IGNORECASE)


def check_baseline(terms):
    """Refuse to measure against a baseline that is not the live filter."""
    mine = matcher_for(terms)
    theirs = scraper._make_matcher(terms)
    if mine.pattern != theirs.pattern:
        sys.exit("baseline matcher diverged from scraper._make_matcher -- fix matcher_for()")


def admitted(rows, inc_re):
    """Indices of the rows this matcher would KEEP, post-exclude and including the reversed
    "Manager, Projects" branch, so the baseline here equals what title_verdict really does."""
    exc = scraper._EXCLUDE_RE
    rev = scraper._REVERSED_RE
    out = set()
    for i, row in enumerate(rows):
        title = row[0]
        if exc.search(title):
            continue
        if inc_re.search(title) or rev.search(title):
            out.add(i)
    return out


def describe(rows, idxs, sample):
    companies = collections.Counter(rows[i][1] or "?" for i in idxs)
    top = ", ".join("%s %d" % (c[:26], n) for c, n in companies.most_common(4))
    seen, egs = set(), []
    for i in idxs:
        t = rows[i][0]
        if t.lower() in seen:
            continue
        seen.add(t.lower())
        egs.append("%s  [%s]" % (t[:52], (rows[i][1] or "?")[:22]))
        if len(egs) >= sample:
            break
    return top, egs, len(companies)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True, help="TSV from scripts/dump_titles.py")
    ap.add_argument("--add", default="", help="comma-separated candidate INCLUDE phrases")
    ap.add_argument("--file", default="", help="file of candidate phrases, one per line")
    ap.add_argument("--variant", choices=("plurals", "separators", "both"), default="",
                    help="score a structural matcher change instead of new phrases")
    ap.add_argument("--sample", type=int, default=6, help="example titles per candidate")
    ap.add_argument("--include-non-us", action="store_true",
                    help="score against non-US postings too (default: US only, as a run stores)")
    a = ap.parse_args()

    rows = load(a.dump, us_only=not a.include_non_us)
    if not rows:
        sys.exit("no rows loaded from %s" % a.dump)

    base = base_terms()
    check_baseline(base)
    kept0 = admitted(rows, matcher_for(base))
    print("dump                     : %s" % a.dump)
    print("postings                 : %d%s"
          % (len(rows), "" if a.include_non_us else " (US only)"))
    print("kept by the filter today : %d (%.1f%%)"
          % (len(kept0), 100.0 * len(kept0) / len(rows)))
    print("the pool to mine         : %d dropped\n" % (len(rows) - len(kept0)))

    if a.variant:
        plur = a.variant in ("plurals", "both")
        seps = a.variant in ("separators", "both")
        gain = admitted(rows, matcher_for(base, plurals=plur, separators=seps)) - kept0
        print("VARIANT %s : +%d row(s)" % (a.variant, len(gain)))
        if gain:
            top, egs, nco = describe(rows, sorted(gain), max(a.sample, 16))
            print("   %d employer(s): %s" % (nco, top))
            for e in egs:
                print("     %s" % e)
        return

    cands = [c.strip().lower() for c in a.add.split(",") if c.strip()]
    if a.file:
        cands += [ln.strip().lower() for ln in io.open(a.file, encoding="utf-8")
                  if ln.strip() and not ln.lstrip().startswith("#")]
    if not cands:
        sys.exit("nothing to score: pass --add, --file or --variant")

    # Greedy: repeatedly take the phrase adding the most rows NOTHING ELSE already admits, so
    # overlapping synonyms are credited once rather than each claiming the same jobs.
    #
    # Each candidate's admits are computed ONCE, against the pool the base filter drops, and the
    # greedy pass then works on those sets. That is exact, not an approximation: INCLUDE is a
    # union, so admits(base + [x, y]) is always admits(base) | admits(x) | admits(y) -- adding a
    # phrase can never take a row away. The naive version re-scanned every row for every
    # (candidate, round) pair, which on this dump is ~2,500 passes over 186k titles.
    exc = scraper._EXCLUDE_RE
    pool = [(i, rows[i][0]) for i in range(len(rows))
            if i not in kept0 and not exc.search(rows[i][0])]
    print("pool after the EXCLUDE veto : %d\n" % len(pool))

    gains = {}
    for c in dict.fromkeys(cands):
        cre = matcher_for([c])
        gains[c] = {i for i, title in pool if cre.search(title)}

    have, order = set(), []
    remaining = [c for c in dict.fromkeys(cands)]
    while remaining:
        best = max(remaining, key=lambda c: len(gains[c] - have))
        gain = gains[best] - have
        order.append((best, gain))
        have |= gain
        remaining.remove(best)
        if not gain:
            # Greedy picked the best remaining and it adds nothing, so nothing left can either.
            order.extend((c, set()) for c in remaining)
            remaining = []

    total = 0
    for phrase, gain in order:
        if not gain:
            continue
        total += len(gain)
        top, egs, nco = describe(rows, sorted(gain), a.sample)
        print("+%-5d %s" % (len(gain), phrase))
        print("        %d employer(s): %s" % (nco, top))
        for e in egs:
            print("          %s" % e)
        print("")
    print("TOTAL new rows           : %d  (+%.1f%% on today's %d)"
          % (total, 100.0 * total / max(len(kept0), 1), len(kept0)))
    dead = [c for c, g in order if not g] + remaining
    if dead:
        print("added nothing            : %s" % ", ".join(sorted(dead)))


if __name__ == "__main__":
    main()
