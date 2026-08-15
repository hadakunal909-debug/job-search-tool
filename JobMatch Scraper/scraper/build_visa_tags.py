#!/usr/bin/env python3
"""
build_visa_tags.py — turn the federal disclosure files into visa_tags.json, the index behind
the H-1B / Green Card / STEM-OPT / E-3 / H-1B1 badges on the feed.

Sources (all downloaded by hand; none are fetched here):
  --lca      DOL OFLC "LCA Programs" .xlsx — EMPLOYER_NAME, TRADE_NAME_DBA, VISA_CLASS.
             VISA_CLASS is one of: H-1B, H-1B1 Chile, H-1B1 Singapore, E-3 Australian.
  --perm     DOL OFLC "PERM" .xlsx — EMP_BUSINESS_NAME, EMP_TRADE_NAME. Green-card sponsorship.
             Note the column is EMP_BUSINESS_NAME, NOT EMPLOYER_NAME like the LCA file.
  --everify  E-Verify employer export .xlsx — Employer, Doing Business As, Account Status.

Output is ONE file, {normalized_employer: bitmask}, not four per-visa files: one loader, one
cache, and no way for one index to drift out of date against another.

    python -m scraper.build_visa_tags --lca "…/LCA_….xlsx" --perm "…/PERM_….xlsx" \
                                      --everify "…/Employer List.xlsx"

WHAT THE TAGS MEAN. Presence says "this employer has filed for this route before". Absence
says nothing at all — one quarter of filings is thin evidence, so the UI must never render a
missing tag as "does not sponsor". Pass --lca/--perm more than once to fold in more quarters.

NAME MATCHING happens HERE, at build time, never at runtime. The feed does a plain dict
lookup on the normalized company name. Resolving 786 company names against ~76k index keys
with rapidfuzz on every cold cache would be ~58M string comparisons and would produce silent
wrong tags with no audit trail; doing it here means every fuzzy decision lands in
visa_tags_report.csv where it can be reviewed and denied.
"""
import argparse
import bisect
import collections
import csv
import datetime
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import _norm_name
from scraper.build_sponsor_counts import ALIASES, GENERIC
from scraper.xlsx_stream import iter_rows

OUT = "visa_tags.json"
REPORT = "visa_tags_report.csv"

BITS = {"h1b": 1, "green_card": 2, "stem_opt": 4, "e3": 8, "h1b1": 16}

# DOL statuses that count. "Certified - Withdrawn" means DOL certified the filing and the
# employer withdrew it later — still evidence they were willing to sponsor, so it counts.
# "Denied" and a bare "Withdrawn" do not.
OK_STATUS = re.compile(r"^certified", re.I)

VISA_CLASS_BIT = {
    "h-1b": "h1b",
    "h-1b1 chile": "h1b1",
    "h-1b1 singapore": "h1b1",
    "e-3 australian": "e3",
}

# A prefix match reaching more spellings than this is unlikely to be one corporate family.
# Set from the data: big employers legitimately file under many entities — Amazon has 11
# ("amazon com services", "amazon web services", "amazon data services", …), Abbott 8,
# Meta 8, Infosys 5. A cap of 8 silently dropped Amazon's H-1B bit entirely.
MAX_FANOUT = 15
# A single filing is enough when we matched the name exactly; a prefix match needs corroboration.
MIN_PREFIX_CASES = 2

# Normalized names that must never be prefix-expanded, because the prefix demonstrably
# reaches a DIFFERENT company. Every entry here was observed in FY2026 Q2, not guessed:
#   nice    -> PERM "nice neat lawncare", "nice recovery systems"
#   hilton  -> 15 separate franchisee entities, none the corporate employer
#   block / flex / stripe / figma / discord / motive / gusto / lattice -> short brand tokens
#           that prefix onto unrelated small firms
# Note these are all short single tokens; longer names are specific enough to expand safely.
# Extra entities are usually harmless (the mask is OR-ed, so a redundant match adds a bit the
# real entity already set) — the danger is only when EVERY match is the wrong company, which
# is exactly this list. Grow it from visa_tags_report.csv.
PREFIX_DENY = {"nice", "hilton", "block", "flex", "stripe", "figma", "discord",
               "motive", "gusto", "lattice", "summit", "quality", "optimal", "galaxy"}


def _prefix_hits(sorted_keys, prefix, cap=MAX_FANOUT + 1):
    """Keys starting with `prefix`, found by binary search on a sorted list.

    Scanning every key for every name is O(names x keys) — with ~77k names against ~30k
    index keys that's billions of startswith() calls and the build never finishes. The keys
    are sorted, so all matches sit in one contiguous run. Stops at `cap`, since anything
    over MAX_FANOUT is discarded anyway.
    """
    i = bisect.bisect_left(sorted_keys, prefix)
    out = []
    while i < len(sorted_keys) and sorted_keys[i].startswith(prefix):
        out.append(sorted_keys[i])
        if len(out) >= cap:
            break
        i += 1
    return out


def norm(s):
    return _norm_name(s or "")


def despace(s):
    return (s or "").replace(" ", "")


class Index(object):
    """Employer names from one source, searchable the three ways a real name can differ."""

    def __init__(self):
        self.cases = collections.Counter()      # norm -> number of certified filings
        self.bits = collections.defaultdict(int)

    def add(self, name, bit, dba=""):
        for raw in (name, dba):
            n = norm(raw)
            if not n or n in ("n a", "na", "none"):
                continue
            self.cases[n] += 1
            self.bits[n] |= BITS[bit]

    def finish(self):
        # de-spaced view recovers "Wal-Mart Associates" for a corpus name of "walmart", and
        # "j p morgan" for "jpmorgan" — spacing differences no prefix match can bridge.
        self.despaced = collections.defaultdict(list)
        for n in self.bits:
            self.despaced[despace(n)].append(n)
        self.keys = sorted(self.bits)
        self.dkeys = sorted(self.despaced)
        return self


def resolve(company, idx, report):
    """Best mask for `company` in one source index, plus how we got there."""
    n = norm(company)
    if not n:
        return 0
    if n in idx.bits:                                            # exact
        return idx.bits[n]

    for alias in ALIASES.get(n, ()):                             # hand-verified alias
        if alias in idx.bits:
            report.append((company, n, alias, "alias", idx.cases[alias]))
            return idx.bits[alias]

    d = despace(n)                                               # spacing-only difference
    if len(d) >= 6 and d in idx.despaced:
        hits = idx.despaced[d]
        mask = 0
        for h in hits:
            mask |= idx.bits[h]
        report.append((company, n, "|".join(hits[:3]), "despaced", sum(idx.cases[h] for h in hits)))
        return mask

    if n.startswith("the ") and n[4:] in idx.bits:               # "The Boeing Company"
        report.append((company, n, n[4:], "the-stripped", idx.cases[n[4:]]))
        return idx.bits[n[4:]]

    if n in GENERIC or len(n) < 4 or n in PREFIX_DENY:           # too risky to expand
        return 0

    pre = _prefix_hits(idx.keys, n + " ")                        # prefix, heavily guarded
    how = "prefix"
    if not pre and len(d) >= 6:
        # De-spaced prefix. Walmart files as "Wal-Mart Associates, Inc." -> "wal mart
        # associates", which neither an exact nor a spaced-prefix match on "walmart" can
        # ever reach; de-spaced, "walmartassociates" starts with "walmart".
        pre = [m for dk in _prefix_hits(idx.dkeys, d) for m in idx.despaced[dk]]
        how = "despaced-prefix"
    if not pre:
        return 0
    if len(pre) > MAX_FANOUT:
        report.append((company, n, "%d spellings" % len(pre), "SKIPPED-fanout", 0))
        return 0
    cases = sum(idx.cases[k] for k in pre)
    if cases < MIN_PREFIX_CASES:
        report.append((company, n, "|".join(pre[:3]), "SKIPPED-thin", cases))
        return 0
    mask = 0
    for k in pre:
        mask |= idx.bits[k]
    report.append((company, n, "|".join(pre[:3]), how, cases))
    return mask


def load_lca(paths):
    idx = Index()
    for p in paths:
        n = 0
        for r in iter_rows(p, ("EMPLOYER_NAME", "TRADE_NAME_DBA", "VISA_CLASS", "CASE_STATUS")):
            if not OK_STATUS.match(r.get("CASE_STATUS") or ""):
                continue
            bit = VISA_CLASS_BIT.get((r.get("VISA_CLASS") or "").strip().lower())
            if not bit:
                continue
            idx.add(r.get("EMPLOYER_NAME"), bit, r.get("TRADE_NAME_DBA"))
            n += 1
        print("  LCA  %-46s %7d certified rows" % (os.path.basename(p)[:46], n))
    return idx.finish()


def load_perm(paths):
    idx = Index()
    for p in paths:
        n = 0
        for r in iter_rows(p, ("EMP_BUSINESS_NAME", "EMP_TRADE_NAME", "CASE_STATUS")):
            if not OK_STATUS.match(r.get("CASE_STATUS") or ""):
                continue
            idx.add(r.get("EMP_BUSINESS_NAME"), "green_card", r.get("EMP_TRADE_NAME"))
            n += 1
        print("  PERM %-46s %7d certified rows" % (os.path.basename(p)[:46], n))
    return idx.finish()


def load_everify(path):
    idx = Index()
    n = 0
    for r in iter_rows(path, ("Employer", "Doing Business As", "Account Status")):
        if (r.get("Account Status") or "").strip().lower() != "open":
            continue
        idx.add(r.get("Employer"), "stem_opt", r.get("Doing Business As"))
        n += 1
    print("  EV   %-46s %7d open employers" % (os.path.basename(path)[:46], n))
    return idx.finish()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lca", action="append", default=[])
    ap.add_argument("--perm", action="append", default=[])
    ap.add_argument("--everify")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--report", default=REPORT)
    ap.add_argument("--universe-only", action="store_true",
                    help="only index employers that appear in our job corpus (tiny file)")
    a = ap.parse_args()
    if not (a.lca or a.perm or a.everify):
        ap.error("give at least one of --lca / --perm / --everify")

    print("reading sources (streamed; the LCA file is ~131 MB)...")
    sources = {}
    if a.lca:
        sources["lca"] = load_lca(a.lca)
    if a.perm:
        sources["perm"] = load_perm(a.perm)
    if a.everify:
        sources["everify"] = load_everify(a.everify)

    # Universe = every employer name in any source, plus every company in our corpus, so a
    # company we start scraping tomorrow still resolves by exact name without a rebuild.
    universe = set()
    for idx in sources.values():
        universe |= set(idx.bits)
    corpus = set()
    try:
        import db
        # cols=: this wants employer names, and a bare load_jobs() buys every JD to get them.
        corpus = {(r.get("company") or "").strip()
                  for r in db.load_jobs(cols=db.COLS_COMPANY)}
        corpus.discard("")
        print("\ncorpus companies: %d" % len(corpus))
    except Exception as e:
        print("\n(no corpus available: %s)" % str(e)[:70])

    names = sorted({norm(c) for c in corpus} | (set() if a.universe_only else universe))
    names = [n for n in names if n]

    report = []
    tags = {}
    for n in names:
        mask = 0
        for key, idx in sources.items():
            # E-Verify gets exact/alias only — never prefix. That file is dominated by small
            # businesses, so prefixes reach the wrong company: "apple" -> "apple foods",
            # "oracle" -> "oracle global supplynet". A wrong bit here is a false claim that an
            # employer is E-Verify enrolled, which a STEM-OPT student would act on.
            if key == "everify":
                if n in idx.bits:
                    mask |= idx.bits[n]
                else:
                    d = despace(n)
                    if len(d) >= 6 and d in idx.despaced:
                        for h in idx.despaced[d]:
                            mask |= idx.bits[h]
            else:
                mask |= resolve(n, idx, report)
        if mask:
            tags[n] = mask

    tags["#meta"] = {
        "built": datetime.datetime.now().isoformat(timespec="seconds"),
        "sources": [os.path.basename(p) for p in (a.lca + a.perm + ([a.everify] if a.everify else []))],
        "bits": BITS,
        "keys": len(tags),
    }
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(tags, fh, separators=(",", ":"), sort_keys=True)

    if report:
        with open(a.report, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["company_norm", "normalized", "matched", "how", "cases"])
            w.writerows(report)

    per = collections.Counter()
    for n, m in tags.items():
        if n == "#meta":
            continue
        for k, b in BITS.items():
            if m & b:
                per[k] += 1
    print("\nwrote %s — %d employers, %.1f MB" % (a.out, len(tags) - 1,
                                                  os.path.getsize(a.out) / 1e6))
    for k in ("h1b", "green_card", "stem_opt", "e3", "h1b1"):
        print("   %-11s %6d employers" % (k, per[k]))
    how = collections.Counter(r[3] for r in report)
    print("\nfuzzy resolutions (%s): %s" % (a.report, dict(how)))

    if corpus:
        hit = sum(1 for c in corpus if tags.get(norm(c)))
        print("corpus coverage: %d of %d companies tagged (%.0f%%)"
              % (hit, len(corpus), 100.0 * hit / len(corpus)))


if __name__ == "__main__":
    main()
