#!/usr/bin/env python3
"""Is the app READING job descriptions correctly? Measured on what we actually store. No database.

The words we pull out of a description are not a cosmetic panel: score_pct is computed over
core.core_terms(analyzed), so a wrong term set is a wrong percentage on every card, a wrong feed
sort, and wrong "keywords worth adding" advice. This is the only place that number is judged
against the real corpus rather than against a fixture.

It reads jd_cache.json.gz -- ~37k real stored descriptions -- so it needs no database, no network
and no credentials. Loading it takes ~15 s; that is the whole cost.

WHY THE PHANTOM TEST IS SPELLED OUT HERE rather than imported. "Phantom" means an ATS keyword the
old bare-substring rule (`kw in jd_low`) fires on which never occurs as a WORD in the posting:
`visio` out of "division", `excel` out of "excellence", `git` out of "digital", `sla` out of
"translate". Defining it independently of core is what keeps the measurement honest AFTER core is
fixed -- the number stays meaningful, it just goes to zero.

BASELINE, 2026-09-02, before any fix (1,500-posting sample of 36,853):

    phantom ATS "hard skills"        86.9% of postings carry at least one
      visio 59.5 / excel 37.1 / sla 24.7 / git 24.0 / safe 20.6 / lean 9.0
    junk share of scored weight      median 17%, mean 18%, worst decile 33%
    page chrome in the description   skip-to-content 3.4 / share-this-job 1.7 /
                                     related-jobs 1.1 / click-the-link-below 0.6
    structure present in jd          newline 9.5%, bullet 10.2%
    text_halves() raises             0.3%

    python scripts/measure_jd_reading.py
    python scripts/measure_jd_reading.py --sample 6000
    python scripts/measure_jd_reading.py --url https://boards.greenhouse.io/foo/jobs/123
    python scripts/measure_jd_reading.py --grep "click the link below" --show 3
    python scripts/measure_jd_reading.py --check
"""
import argparse
import gzip
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import jdrender

CACHE = "jd_cache.json.gz"

# THE APP'S OWN FILTER, imported rather than copied. This script used to keep its own transcript
# of web.py's two stoplists because it must not import web (analytics.py reads EV_OFF once at
# import, and one unguarded run wrote 98.8% of all recorded feed_view events). They have since
# moved into core.display_terms, so the copy became a SECOND definition that drifted: it still
# hid c# and c++ on the old three-character rule, which core now exempts for a known hard skill,
# and so it overstated the junk it was measuring. One definition.

# Inflections a real ATS keyword match may carry. Deliberately NOT "ly": allowing it would
# recover "cross-functionally" (711 postings) and resurrect "safely" -> SAFe (1,044).
_INFLECT = r"(?:s|es|ing|ed|er|ers|or|ors|ies|ment|ments|ation|ations)?"

# Whole-page furniture that means core.fetch_jd read the PAGE instead of the POSTING.
CHROME = (
    ("'skip to main content'", r"skip to (?:main )?content"),
    ("'share this job'", r"share this job|share on (?:facebook|linkedin|twitter)"),
    ("'related / similar jobs'", r"related jobs|similar jobs|other jobs you may"),
    ("'click the link below'", r"click (?:the |on the )?link below"),
    ("'enable JavaScript'", r"enable javascript"),
    ("cookie / consent bar", r"cookie(?:s)? (?:policy|settings|preferences)|accept all cookies"),
    ("apply-portal sign-in", r"sign in to (?:apply|your account)|create (?:an )?account to apply"),
)

# Short tech names the tokenizer drops today: core.py strips "-.+#/" then requires len(t) > 2.
SHORT_TECH = (
    ("c++", r"c\+\+"),
    ("c#", r"c#"),
    ("ci/cd", r"ci/cd"),
    (".net", r"\.net"),
    ("go", r"(?<![a-z])go(?![a-z])"),
)


def _stop_set():
    """Kept as a function so the call sites below read the same; core owns the lists now."""
    return set(core.SKILL_STOP) | set(core.KEYWORD_STOP) | set(core.PERK_TERMS)


def _boundary_rx(kw):
    return re.compile(r"(?<![a-z0-9])" + re.escape(kw) + _INFLECT + r"(?![a-z0-9])")


_BRX = {kw: _boundary_rx(kw) for kw in core.ATS_KEYWORDS}


# SINGLE, UNPUNCTUATED WORDS ONLY -- that is the error class. A phantom is a short word hiding
# inside a longer unrelated one: visio/"division", excel/"excellence", sla/"translate",
# git/"digital", safe/"safety", lean/"cleaning". Those six were 89% of the problem.
#
# Phrases and punctuated names are deliberately excluded, because there a substring hit is not
# the same mistake: "cross-functionally" does name cross-functional work and "asp.net" does name
# .NET, so counting those as phantoms measured this script's strictness rather than the app's
# accuracy. core._term_in decides them on the phrase branch, on purpose.
_PHANTOM_KW = tuple(k for k in core.ATS_KEYWORDS
                    if " " not in k and not any(c in k for c in "+#./-"))


def phantoms(jd_low):
    """ATS keywords the bare-substring rule admits which are not WORDS in this posting."""
    return [kw for kw in _PHANTOM_KW if kw in jd_low and not _BRX[kw].search(jd_low)]


def halves(jd):
    """(body, legal, raised). jdrender.text_halves is expected to raise on the kv node kind."""
    try:
        body, legal = jdrender.text_halves(jd)
        return body, legal, False
    except Exception:
        return (jd or "").lower(), "", True


def judge(jd, idf, stop):
    """One posting -> (analyzed, core_terms, phantoms, {term: why}, junk, raised, live).

    `phantoms` is what the OLD substring rule would invent. It is a fixed historical yardstick
    and stays high forever -- that is the point of keeping the rule here rather than importing
    it. `live` is the subset that still reaches analyze_jd's own term list, which is the number
    that has to go to zero.
    """
    low = (jd or "").lower()
    ph = set(phantoms(low))
    a = core.analyze_jd(jd, idf)
    live = ph & {(t or "").lower() for t in (a.get("terms") or [])}
    ct = core.core_terms(a)
    body, legal, raised = halves(jd)
    why = {}
    for t in ct:
        lo = (t or "").lower()
        if lo in ph:
            why[t] = "phantom"
        elif not core.display_terms([t], "", 1, body=body, boiler=legal):
            # ASK THE APP, do not re-derive it. core.display_terms is what /job, /tailor and
            # /brain/tailor all run, so "junk" here means exactly "a term the app would refuse
            # to show a reader" -- which is the only definition that cannot drift.
            why[t] = "refused"
    tot = sum(a["weight"].get(t, 0.0) for t in ct) or 1.0
    junk = sum(a["weight"].get(t, 0.0) for t in why) / tot
    return a, ct, sorted(ph), why, junk, raised, sorted(live)


def show_one(url, jd, idf, stop):
    a, ct, ph, why, junk, raised, live = judge(jd, idf, stop)
    print("\n%s" % url)
    print("  %d chars | %s | %s | thin=%s | text_halves %s"
          % (len(jd), "newlines" if "\n" in jd else "FLAT",
             "bullets" if "•" in jd else "no bullets",
             a.get("thin"), "RAISED" if raised else "ok"))
    if ph:
        print("  the old substring rule would have invented: %s" % ", ".join(ph))
    print("  phantoms still reaching analyze_jd: %s" % (", ".join(live) if live else "none"))
    print("  junk share of the scored weight: %.0f%%" % (100.0 * junk))
    print("  the terms the match %% is actually computed over, heaviest first:")
    for t in sorted(ct, key=lambda x: -a["weight"].get(x, 0.0)):
        print("      %7.2f  %-34s %s" % (a["weight"].get(t, 0.0), t, why.get(t, "")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=1500)
    ap.add_argument("--seed", type=int, default=7, help="fixed so runs stay comparable")
    ap.add_argument("--url", action="append", default=[], help="explain one posting; repeatable")
    ap.add_argument("--grep", help="explain postings whose text contains this")
    ap.add_argument("--show", type=int, default=2, help="how many --grep hits to explain")
    ap.add_argument("--check", action="store_true", help="exit 1 if a threshold is breached")
    ap.add_argument("--max-phantom", type=float, default=1.0, help="%% of postings, for --check")
    ap.add_argument("--max-junk", type=float, default=8.0, help="median %%, for --check")
    a = ap.parse_args()

    app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(app, CACHE)
    if not os.path.exists(path):
        print("no %s here -- this script measures the LOCAL cache; nothing to do." % CACHE)
        return 0
    jds = json.load(gzip.open(path, "rt", encoding="utf-8"))
    idf = core.load_idf()
    stop = _stop_set()

    if a.url or a.grep:
        for u in a.url:
            if u in jds:
                show_one(u, jds[u], idf, stop)
            else:
                print("\n%s -- not in the local cache" % u)
        if a.grep:
            hits = [u for u, j in jds.items() if j and a.grep.lower() in j.lower()]
            print("\n%d of %d stored descriptions contain %r" % (len(hits), len(jds), a.grep))
            for u in hits[:a.show]:
                show_one(u, jds[u], idf, stop)
        return 0

    usable = [u for u, j in jds.items() if j and len(j) >= core._MIN_JD_CHARS]
    random.seed(a.seed)
    sample = random.sample(usable, min(a.sample, len(usable)))
    n = len(sample)
    print("%d stored descriptions; %d usable (>=%d chars); measuring %d (seed %d)\n"
          % (len(jds), len(usable), core._MIN_JD_CHARS, n, a.seed))

    crx = [(k, re.compile(rx)) for k, rx in CHROME]
    srx = [(k, re.compile(rx)) for k, rx in SHORT_TECH]
    chrome = {k: 0 for k, _rx in CHROME}
    lost = {k: [0, 0] for k, _rx in SHORT_TECH}
    struct = {"newline": 0, "bullet": 0}
    ph_jobs = live_jobs = raised = capped = 0
    ph_terms, live_terms, junk_terms, junks = {}, {}, {}, []

    for u in sample:
        jd = jds[u]
        low = jd.lower()
        for k, rx in crx:
            if rx.search(low):
                chrome[k] += 1
        if "\n" in jd:
            struct["newline"] += 1
        if "•" in jd:
            struct["bullet"] += 1
        if len(jd) >= 7990:
            capped += 1
        _a, ct, ph, why, junk, r, live = judge(jd, idf, stop)
        raised += bool(r)
        if ph:
            ph_jobs += 1
            for p in ph:
                ph_terms[p] = ph_terms.get(p, 0) + 1
        if live:
            live_jobs += 1
            for q in live:
                live_terms[q] = live_terms.get(q, 0) + 1
        if not ct:
            continue
        junks.append(100.0 * junk)
        for t in why:
            junk_terms[t] = junk_terms.get(t, 0) + 1
        scored = {(t or "").lower() for t in ct}
        for k, rx in srx:
            if rx.search(low):
                lost[k][0] += 1
                if k not in scored:
                    lost[k][1] += 1

    def pct(c):
        return 100.0 * c / n

    junks.sort()
    med = junks[len(junks) // 2] if junks else 0.0
    p90 = junks[int(len(junks) * 0.9)] if junks else 0.0
    mean = sum(junks) / len(junks) if junks else 0.0

    print("A) PHANTOM ATS 'hard skills'")
    print("     the old substring rule would invent one in %.1f%% of postings (%d) -- the"
          % (pct(ph_jobs), ph_jobs))
    print("     fixed yardstick, not a regression:")
    for t, c in sorted(ph_terms.items(), key=lambda kv: -kv[1])[:8]:
        print("        %-14s %5.1f%%" % (t, pct(c)))
    print("     STILL REACHING analyze_jd: %.1f%% of postings (%d)  <-- THIS is the one"
          % (pct(live_jobs), live_jobs))
    for t, c in sorted(live_terms.items(), key=lambda kv: -kv[1])[:8]:
        print("        %-14s %5.1f%%" % (t, pct(c)))

    print("\nB) JUNK SHARE OF THE SCORED WEIGHT -- noise driving the match %")
    print("     median %.0f%%   mean %.0f%%   worst decile %.0f%%" % (med, mean, p90))
    for t, c in sorted(junk_terms.items(), key=lambda kv: -kv[1])[:12]:
        print("        %-28s %5.1f%%" % (t, pct(c)))

    print("\nC) WHOLE-PAGE CHROME read as the job description")
    for k, _rx in CHROME:
        print("     %-28s %5.1f%%  (%d)" % (k, pct(chrome[k]), chrome[k]))

    print("\nD) STRUCTURE PRESENT IN THE STORED TEXT (Stage 4 progress)")
    print("     contains a newline %5.1f%%      contains a bullet %5.1f%%"
          % (pct(struct["newline"]), pct(struct["bullet"])))
    print("     within 10 chars of the 8000 cap %.1f%%" % pct(capped))

    print("\nE) SHORT TECH NAMES: named in the posting, absent from the scored terms")
    for k, _rx in SHORT_TECH:
        seen, missed = lost[k]
        if seen:
            print("     %-8s named in %4d postings, dropped from the terms in %4d (%.0f%%)"
                  % (k, seen, missed, 100.0 * missed / seen))

    print("\nF) jdrender.text_halves() RAISES -> the legal filter is silently off")
    print("     %.1f%% of postings  (%d)" % (pct(raised), raised))

    if a.check:
        bad = []
        if pct(live_jobs) > a.max_phantom:
            bad.append("phantom ATS terms still reaching analyze_jd in %.1f%% of postings "
                       "(max %.1f)" % (pct(live_jobs), a.max_phantom))
        if med > a.max_junk:
            bad.append("median junk share %.0f%% (max %.0f)" % (med, a.max_junk))
        if raised:
            bad.append("text_halves raised on %d postings" % raised)
        if bad:
            print("\nFAILED: " + "; ".join(bad))
            return 1
        print("\nOK: live phantoms %.1f%% <= %.1f, junk median %.0f%% <= %.0f, "
              "no text_halves raises" % (pct(live_jobs), a.max_phantom, med, a.max_junk))
    return 0


if __name__ == "__main__":
    sys.exit(main())
