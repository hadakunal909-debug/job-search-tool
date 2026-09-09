#!/usr/bin/env python3
"""
build_resume_vocab.py — build the two offline data files the résumé review panel needs.

    python scripts/build_resume_vocab.py                # both
    python scripts/build_resume_vocab.py --only vocab   # spelling only
    python scripts/build_resume_vocab.py --only keywords --show 60

Writes, at the repo root:

  resume_vocab.json     {"min_df": N, "words": [...]}         -- the spelling vocabulary
  resume_keywords.json  {"dev": [[term, df_ratio], ...], ...} -- expected hard skills per track

Both are OPTIONAL at runtime: resume_score's spelling check and resume_keywords' per-track
expectations go dormant without them rather than failing, which is the same contract
core.load_sponsor_counts already has. Re-run this after a scrape to refresh.

WHY THIS EXISTS, for each file:

*Spelling.* There is no spellchecker installed and no word list on disk, so the alternatives were a
new pip dependency or committing a generic English dictionary. Both are worse than mining our own
23k job descriptions, because the words a generic dictionary flags as errors — Kubernetes, Jaggaer,
IntelliBuy, Workday — are exactly the words that saturate a corpus of real postings. A résumé is
written in work English, so work English is the right dictionary.

*Keywords.* `idf.json` holds 622k weighted terms but has no notion of skill-ness: "block logo"
outranks `pmp`. So curation decides what counts as a skill and the corpus decides which skills
matter for which track. Terms come from `jd_terms`, the packed weight map the scraper ALREADY
computed per posting (core.pack_analyzed) — so this agrees with the feed's matcher by construction
instead of re-deriving terms with a second, subtly different extractor.

The measure is DOCUMENT FREQUENCY, not IDF. IDF rewards rarity, and an expectation is the opposite
of rare: "which skills do most postings in this track ask for" is a df question. Terms above
_TOO_COMMON are dropped as boilerplate rather than kept as strong signals.
"""
import argparse
import gzip
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core                                                   # noqa: E402
import resume_keywords                                        # noqa: E402  (for _SOFT)

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(APP, "jobs_snapshot.json.gz")
JD_CACHE = os.path.join(APP, "jd_cache.json.gz")
VOCAB_OUT = os.path.join(APP, "resume_vocab.json")
KEYWORDS_OUT = os.path.join(APP, "resume_keywords.json")

# ---- spelling vocabulary ----
# A word has to appear in this many DISTINCT postings to be trusted. Document frequency, not raw
# count: one posting that repeats a typo forty times must not teach us the typo.
# THE SPELLING FLOOR IS RELATIVE, because as an absolute it rots. 5 was calibrated against
# 22,997 postings; at 48,353 the SAME absolute floor admits proportionally more noise, and
# three misspellings cleared it -- managment (df 8), enviroment (df 5) and responsibilites
# (df 22) became "known words", so resume_score._nearest could no longer offer a correction for
# them. test_resume_score caught managment; the other two it did not ask about.
#
# MEASURED 2026-09-08 over 48,353 cached descriptions: df>=10 drops managment and enviroment
# and costs NO real vocabulary (jaggaer, the rarest genuine term checked, first disappears at
# 12). 5/22,997 is 0.0217%, which at 48,353 is 10.5 -- so 10 is not a new judgement, it is the
# original one restated so it survives the corpus growing again.
#
# responsibilites (df 22) STILL SURVIVES and is stated rather than hidden: it needs df>=25,
# which is where real words start going. A frequency floor cannot separate a common typo from
# a rare word; that needs a dictionary, which this module deliberately does not have (see the
# note on _nearest -- absence is weak evidence in a 22k-word corpus).
VOCAB_MIN_DF_PER_POSTING = 5 / 22997.0
VOCAB_MIN_DF_FLOOR = 5              # never looser than the original, however small a corpus
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")

# ---- keyword expectations ----
KEYWORDS_PER_TRACK = 120        # written out; resume_keywords.TOP_N decides how many are scored
_TOO_RARE = 0.015               # under 1.5% of a track's postings: not an expectation
_TOO_COMMON = 0.55              # over 55%: boilerplate ("teams", "business"), not a skill
_MIN_TERM_LEN = 3
# How much more often one track must ask for a term than the other before it counts as a
# skill rather than filler. 1.4 was chosen by looking at what it admits and rejects, not
# analytically -- see the discrimination note in build_keywords.
# RE-MEASURED FOR THREE TRACKS, 2026-09-08. It was 1.4, chosen by eye at TWO tracks, and the
# note in build_keywords already said a third bucket makes `lo` smaller for most terms so the
# same ratio gets MORE permissive. It did, and the comment claimed the re-measurement had
# happened when it had not -- caught by reading the shipped panel, which offered "business",
# "teams", "customers", "management", "development", "technology" and "technical" as skills a
# product-track posting asks for. 13 of the 14 boilerplate words in the top 40 were admitted
# HERE, not by curation.
#
# MEASURED discrimination at three tracks: business 2.29, teams 2.09, customers 2.04,
# development 2.12, technology 2.30, management 2.67, technical 2.84 -- against curated real
# skills that BYPASS this test entirely (product strategy 48.8, a/b test 28.6, product roadmap
# 25.5, go-to-market 20.0, experimentation 14.9, backlog 13.3, user stories 12.7).
#
# 3.0 is therefore close to free: every genuine skill in that list is curated and immune, so
# this only ever culls terms that earned their place on a ratio.
_MIN_DISCRIMINATION = 3.0

# WHAT DISCRIMINATION CANNOT FIX, and the reason it cannot.
#
# Raising the ratio to 3.0 culled ten boilerplate words. These survived it because they really
# ARE distinctive of one track -- and that is precisely the problem: being distinctive is not
# being a skill. Measured at three tracks: product 4.25, products 8.32, market 17.23,
# define 16.91, customer 4.15.
#
# "product" and "products" are TAUTOLOGICAL. The product track is defined by
# core.roles_for_title containing "product", so the word firing hardest inside it says nothing
# about which skill a posting wants. No ratio can see that; only knowing how the bucket was
# built can.
#
# NOT ADDED TO core.JD_BOILERPLATE, deliberately: _candidate_terms shares that set and builds
# idf.json from it, so a display fix there would silently re-weight the entire scoring
# vocabulary -- the same trap analyze_jd's extra_skip note describes. This list is local to the
# panel it fixes.
#
# Curated terms are NOT exempted from this one. "operations" is in ATS_DOMAIN and is still
# employer boilerplate when offered to a reader as a skill to demonstrate.
_NOT_A_SKILL = frozenset((
    "product", "products",                      # tautological: the track is named after them
    "market", "define", "needs", "user", "users", "capabilities", "customer", "customers",
    "solutions", "strategy", "platform", "delivery", "operations", "business", "teams",
    "technology", "technical", "development", "management", "engineering", "data", "through",
))


def _rows():
    with gzip.open(SNAPSHOT, "rt", encoding="utf-8", errors="replace") as fh:
        snap = json.load(fh)
    return snap.get("rows") if isinstance(snap, dict) else (snap or [])


def build_keywords(show=0):
    """Document frequency per (track, term), from the packed jd_terms the scraper already wrote."""
    rows = _rows()
    df = {t: Counter() for t in resume_keywords.TRACKS}
    n = Counter()
    skipped = 0
    for r in rows:
        # PRODUCT IS ITS OWN TRACK as of 2026-09-08. core.role_track only ever answers dev or
        # mgmt, so this refines it: a title the product family claims is product work, and
        # everything else falls through to the old two-way answer. roles_for_title is memoised
        # (core._role_cache) so this costs nothing over a whole-snapshot pass.
        #
        # Note "Product Owner" moves out of mgmt and into product, because the product family
        # lists it. That is intended -- it is the same job.
        title = r.get("title") or ""
        track = "product" if "product" in core.roles_for_title(title) else core.role_track(title)
        n[track] += 1
        packed = r.get("jd_terms") or ""
        if not packed:
            skipped += 1
            continue
        try:
            w = (json.loads(packed) or {}).get("w") or {}
        except Exception:
            skipped += 1
            continue
        # set(): df counts postings, not mentions.
        for term in set(w):
            df[track][term] += 1

    stop = set(getattr(core, "STOPWORDS", ())) | set(getattr(core, "JD_BOILERPLATE", ()))
    soft = resume_keywords._SOFT
    curated = set(getattr(core, "ATS_KEYWORDS", ())) | set(getattr(core, "SKILLS", {}))
    curated = set(c.lower() for c in curated)

    # THE NOISE FILTER, and the reason it is not a hand-written stop list.
    #
    # Document frequency alone kept "time", "through", "business", "teams", "qualifications" —
    # generic nouns that saturate every posting and say nothing about a skill. Listing them by hand
    # is endless and arbitrary. Discrimination is the principled version: a skill is demanded
    # DIFFERENTLY by different work. "python" is far more common in dev postings than mgmt,
    # "project management" the reverse, while "time" and "business" sit at the same rate in both —
    # so the ratio between the two tracks separates skills from filler without anyone curating it.
    #
    # Curated terms bypass this: "excel" is genuinely asked for at similar rates everywhere, and it
    # is unambiguously a skill because a human already said so.
    # GENERALISED to len(TRACKS) on 2026-09-08, and _MIN_DISCRIMINATION had to be re-measured
    # rather than carried over: a third bucket makes `lo` smaller for most terms, so the SAME
    # ratio becomes MORE permissive exactly where it needs to be tighter. The number below is
    # what the --show sweep admits and rejects at three tracks, chosen the same way the 1.4 was.
    def discrimination(t):
        rates = [df[k][t] / float(max(1, n[k])) for k in resume_keywords.TRACKS]
        hi, lo = max(rates), min(rates)
        return hi / lo if lo > 0 else float("inf")

    out = {}
    for track in resume_keywords.TRACKS:
        total = max(1, n[track])
        keep = []
        for term, c in df[track].items():
            t = term.strip().lower()
            ratio = c / float(total)
            if len(t) < _MIN_TERM_LEN or t in stop or t in soft:
                continue
            if not (_TOO_RARE <= ratio <= _TOO_COMMON):
                continue
            # A term made only of stopwords ("clear ownership" survives, "of the" does not).
            if all(p in stop for p in t.split()):
                continue
            if not any(ch.isalpha() for ch in t):
                continue
            # Bare boilerplate goes regardless of curation -- see _NOT_A_SKILL.
            if t in _NOT_A_SKILL:
                continue
            if t not in curated and discrimination(t) < _MIN_DISCRIMINATION:
                continue
            keep.append((t, round(ratio, 4)))
        keep.sort(key=lambda kv: (-kv[1], kv[0]))
        out[track] = keep[:KEYWORDS_PER_TRACK]
        print("  %-5s %6d postings, %6d terms seen, %4d kept"
              % (track, total, len(df[track]), len(out[track])))
        if show:
            print("        top: %s" % ", ".join(t for t, _ in out[track][:show]))
    if skipped:
        print("  (%d postings had no packed jd_terms and were skipped)" % skipped)
    with open(KEYWORDS_OUT, "w", encoding="utf-8") as fh:
        # _meta carries the corpus size so the Keywords panel can state it instead of hard-coding
        # a figure in the template, where it went stale the first time the feed grew. Namespaced
        # with a leading underscore so it can never collide with a track name.
        json.dump(dict(out, _meta={"jobs": int(sum(n.values()))}), fh,
                  ensure_ascii=False, separators=(",", ":"))
    print("  wrote %s (%.0f KB)" % (KEYWORDS_OUT, os.path.getsize(KEYWORDS_OUT) / 1024.0))
    return out


def build_vocab():
    """Words seen in at least VOCAB_MIN_DF distinct job descriptions."""
    df = Counter()
    n = 0
    with gzip.open(JD_CACHE, "rt", encoding="utf-8", errors="replace") as fh:
        cache = json.load(fh)
    for val in cache.values():
        text = val if isinstance(val, str) else " ".join(
            str(v) for v in (val or {}).values() if isinstance(v, str))
        if not text:
            continue
        n += 1
        for w in set(m.group(0).lower().strip("'-") for m in _WORD_RE.finditer(text)):
            if len(w) >= 3:
                df[w] += 1
    # Derived from the corpus actually scanned, not a constant -- see the note on
    # VOCAB_MIN_DF_PER_POSTING.
    min_df = max(VOCAB_MIN_DF_FLOOR, int(round(n * VOCAB_MIN_DF_PER_POSTING)))
    words = sorted(w for w, c in df.items() if c >= min_df)
    with open(VOCAB_OUT, "w", encoding="utf-8") as fh:
        json.dump({"min_df": min_df, "postings": n, "words": words}, fh,
                  ensure_ascii=False, separators=(",", ":"))
    print("  %d postings, %d distinct words, %d kept at df>=%d"
          % (n, len(df), len(words), min_df))
    print("  wrote %s (%.0f KB)" % (VOCAB_OUT, os.path.getsize(VOCAB_OUT) / 1024.0))
    return words


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("vocab", "keywords"))
    ap.add_argument("--show", type=int, default=25, help="print the top N kept keywords")
    a = ap.parse_args()
    if a.only != "vocab":
        print("keyword expectations per role track:")
        build_keywords(a.show)
    if a.only != "keywords":
        print("spelling vocabulary:")
        build_vocab()


if __name__ == "__main__":
    main()
