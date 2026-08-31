#!/usr/bin/env python3
"""Find companies that are hiring PM/product roles RIGHT NOW and that we do not scrape yet.

WHY THIS EXISTS, and why it is not another pass over the E-Verify list. The 2026-08-16 run
probed 5,419 enrolled employers and produced ONE job above the match floor, because an
enrolment list is mostly small businesses: 95% have never filed an LCA and ~99% have no
ATS-backed board, and a company with no board is not hiring a product manager. Its own
conclusion was that the leverage is employers already known to be hiring. This script starts
there instead -- from a live posting feed -- and keeps only the employer NAME. The posting text
is thrown away, which is what keeps CLAUDE.md's "don't reach for a job aggregator" intact: an
aggregator is a discovery channel here, never a feed source. Whatever survives is scraped from
the employer's own board by the existing probe/adopt chain, so descriptions stay usable.

STAGE 1 of four. The other three already exist:
    2 screen  -- this file, offline, below
    3 probe   -- scripts/probe_discovered.py  -> find_everify_boards.discover()
    4 adopt   -- scraper/adopt_everify_boards.py -> db.add_board

HARVEST IS AN INTERFACE, NOT A LINKEDIN FUNCTION. Every channel returns the same record shape
({company, title, url, location, posted, channel, phrase}), so adding or swapping one is a new
function and a --channel value, never a redesign. `phrase` is the QUERY that returned the row and
is empty for a channel that has no query (--channel csv). That matters because no free channel
is complete: LinkedIn rate-limits an unauthenticated IP within a few hundred results, so a run
yields low hundreds of employers, not thousands. Composing channels is the only way to volume.

    python scripts/discover_companies.py --channel linkedin --hours 24
    python scripts/discover_companies.py --channel indeed,google --hours 168
    python scripts/discover_companies.py --channel csv --in names.txt   # any external list
    python scripts/discover_companies.py --channel linkedin --results 25 -v   # smoke test

WRITES NOTHING but its own CSV. There is deliberately NO --apply flag, the same guarantee
scripts/jobspy_shadow.py makes: adoption is a separate, reviewable step.
"""
import argparse
import collections
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import scraper
from scraper import classify_everify as ce
from scraper import find_everify_boards as feb

REPORT = "discovered_companies.csv"

FIELDNAMES = ("company", "channel", "harvest_phrases", "postings_seen", "pm_titles_kept",
              "sample_title", "in_sources", "sources_name", "in_boards_table", "h1b_filings",
              "match_method", "visa_tags", "stem_opt", "cap_exempt", "bodyshop")
# pm_titles_kept is a HISTORICAL name, not a PM-only count -- the gate is scraper.title_verdict,
# the same full include/exclude filter the sweep uses. Do NOT rename it: both readers reach for
# `r.get("pm_titles_kept") or 0`, so a rename makes every CSV written before it read as zero,
# silently, and --min-pm 1 would then drop every row of an older file.

# JobSpy sites that are worth asking. Kept here rather than read from JOBSPY_SITES because that
# env var switches the real SCRAPE on, and this script must never depend on it being set.
JOBSPY_SITES = ("linkedin", "indeed", "google", "glassdoor", "zip_recruiter")

# The PM family. Deliberately narrower than scraper.INCLUDE: this is the harvest QUERY, and a
# broader query returns the same employers plus noise. Breadth is applied later instead --
# adopt_everify_boards judges a board with the FULL title filter, so a company found on a PM
# query still counts for the data/AI/SWE roles it also posts.
PM_PHRASES = ("project manager", "product manager", "program manager",
              "technical program manager", "product owner", "scrum master")

# The 2026-08-30 widening: every role track the title filter already admits, not just the PM
# family. Kept here rather than pasted onto a command line because the per-role report NAMES
# these phrases -- a report whose inputs live only in shell history cannot be re-run against.
# Every entry was checked through scraper.title_verdict() before being added. "solutions
# architect" and "cloud architect" are deliberately ABSENT: title_verdict excludes them
# ("off-target function ('Architect')"), so those queries cannot contribute a scored job.
ROLE_PHRASES = ("business analyst", "systems analyst", "product analyst",
                "business intelligence analyst",
                "software engineer", "software developer", "backend engineer",
                "frontend engineer", "full stack engineer",
                "data analyst", "data engineer", "data scientist",
                "machine learning engineer", "ai engineer",
                "qa engineer", "devops engineer", "cloud engineer",
                "technical product manager")

ALL_PHRASES = PM_PHRASES + ROLE_PHRASES

# Column names an external list might use for the employer. Checked in order.
_NAME_COLS = ("company", "employer", "name", "company_name", "employer_name", "organization")


def unusable_name(name):
    """True when `name` is a single generic corporate word and so cannot identify an employer.

    Indeed returned a posting whose company was literally "International". Nothing downstream
    can recover from that: _careers_candidates guesses careers.international.com, and the probe
    duly found UpWork's board, which adopt_everify_boards then graded `review` against the
    reported name "UpWork International" and refused. So the safeguard held -- this only saves
    the wasted requests and stops a junk row reaching the report.

    DELIBERATELY NARROW: single token only, and only when that token is in scraper's own
    _GENERIC_NAME_WORDS. The obvious wider rule -- drop anything whose every token is generic --
    was measured against the 1,341 real names first and would have thrown out **Bank of
    America** ({bank, of, america}) and **Health Care Service Corporation**. Short names are not
    the signal either: EY, IBM, HP, AMD, 3M, UPS, TD, RTX, HEB and PNC all arrive this way and
    are real. On the same 1,341 names this rule drops exactly one, with no false positives.
    """
    toks = [t for t in scraper._strict_norm_name(name or "").split() if t]
    return len(toks) == 1 and toks[0] in scraper._GENERIC_NAME_WORDS


# ---------------------------------------------------------------------------------- harvest

def harvest_jobspy(sites, phrases, location, hours, results, verbose=False):
    """One record per posting, via the adapter the scraper already ships.

    Reuses scraper.scrape_jobspy rather than calling the library directly so the selector
    format, the URL preference order (_jobspy_best_url) and the per-query truncation note stay
    in one place. The module globals it reads are set here for the duration of the run only --
    nothing is persisted and JOBSPY_SITES stays empty, so SOURCES is unaffected.
    """
    # Check the library ONCE, up front, and say so plainly. scrape_jobspy's own ImportError path
    # returns [] with a note, which is right for a scrape (a dormant source must not fail a run)
    # and wrong here: five phrases print five notes and the run then reports "0 postings", which
    # reads exactly like the site refused us. Two very different problems, one indistinguishable
    # symptom. Note the install is not just `pip install -r requirements.txt`: python-jobspy
    # 1.1.82 hard-pins numpy==1.26.3, which has no wheels past cp312, so on a newer interpreter
    # it must go in with --no-deps plus a current numpy/pandas.
    try:
        import jobspy                                              # noqa: F401
    except ImportError:
        raise SystemExit(
            "python-jobspy is not installed, so no aggregator channel can return anything.\n"
            "  pip install --no-deps python-jobspy==1.1.82\n"
            "  pip install beautifulsoup4 markdownify pandas pydantic regex requests tls-client\n"
            "Or use --channel csv --in <file> to screen a list obtained some other way.")

    scraper.JOBSPY_RESULTS = results
    scraper.JOBSPY_HOURS_OLD = hours
    scraper.JOBSPY_LINKEDIN_JD = False    # one extra request per job = the fastest way to a block
    out = []
    for site in sites:
        for phrase in phrases:
            selector = "jobspy:%s|%s|%s" % (site, phrase, location)
            try:
                rows = scraper.scrape_jobspy(selector)
            except Exception as e:
                print("  %-14s %-26s ERROR %s: %s" % (site, phrase, type(e).__name__, e))
                continue
            for r in rows:
                out.append({"company": r.get("company") or "",
                            "title": r.get("title") or "",
                            "url": r.get("url") or "",
                            "location": r.get("location") or "",
                            "posted": r.get("found_date") or "",
                            "channel": site,
                            "phrase": phrase})
            if verbose:
                print("  %-14s %-26s %4d rows" % (site, phrase, len(rows)))
    return out


def _name_from_row(row):
    """The employer name out of a DictReader row, whatever the source called that column."""
    lowered = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
    for col in _NAME_COLS:
        if lowered.get(col):
            return lowered[col]
    return ""


def harvest_csv(path, channel="csv"):
    """Records from an external list -- one company name per line, or a CSV with a name column.

    This is the door every non-JobSpy channel comes through: a company-level API (TheirStack),
    a government feed (CareerOneStop, USAJOBS), or a list pulled by hand. Such a list usually
    carries no title, so pm_titles_kept stays 0 and harvest_phrases stays empty for these rows,
    and the probe queue ranks them on sponsor evidence alone.
    """
    if not os.path.exists(path):
        raise SystemExit("no such file: %s" % path)
    with open(path, encoding="utf-8-sig", newline="") as f:   # utf-8-sig: the BOM otherwise
        text = f.read()                                       # glues itself to column one
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []

    out = []
    header = lines[0].lower()
    if "," in lines[0] and any(c in header for c in _NAME_COLS):
        for r in csv.DictReader(lines):
            name = _name_from_row(r)
            if name:
                out.append({"company": name, "title": (r.get("title") or "").strip(),
                            "url": "", "location": "", "posted": "", "channel": channel,
                            "phrase": ""})
    else:
        for ln in lines:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append({"company": ln, "title": "", "url": "", "location": "",
                            "posted": "", "channel": channel, "phrase": ""})
    return out


# ----------------------------------------------------------------------------------- screen

def _fmt_phrases(counter):
    """One cell: "product manager:12|scrum master:3" -- which queries returned this employer.

    THE COUNT IS IN THE CELL, not just the phrase, because without it the per-role report can
    say how many employers a phrase surfaced but not how much volume it carried -- and widening
    6 phrases to 24 is being done precisely to find out which of the new ones pay for a query.

    The counts SUM TO postings_seen for an aggregator row, which is what makes this column
    checkable rather than decorative: harvest does not dedupe across queries, so a posting
    returned by both "program manager" and "technical program manager" is genuinely two hits
    and postings_seen already counts it twice.

    Sorted by count desc so the dominant role reads first. ':' is squashed because it is the
    separator; '|' cannot occur -- --phrases already split the caller's list on it.
    """
    return "|".join("%s:%d" % (p.replace(":", " ").strip(), n)
                    for p, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def parse_phrases(cell):
    """{phrase: count} back out of a harvest_phrases cell, for any reader of the CSV.

    Tolerates a cell that is missing entirely, which is not hypothetical: discovered_2wk.csv,
    discovered_month.csv and every CSV written before 2026-08-30 predate this column and must
    keep reading as "no phrase data", never as a crash.
    """
    out = {}
    for part in (cell or "").split("|"):
        part = part.strip()
        if not part:
            continue
        name, _, n = part.rpartition(":")
        if not name:                      # a bare phrase with no count -- read it as one hit
            name, n = part, "1"
        out[name.strip()] = int(n) if n.strip().isdigit() else 0
    return out


def screen(records):
    """Collapse postings to one row per employer and tag each with what we already know.

    Entirely offline: the sponsor and visa answers are local dict lookups, so "does this company
    support STEM OPT" costs nothing per company. Note what that answer is worth, though --
    visa_tags' stem_opt bit came from the E-Verify+ PILOT export, not the E-Verify registry, and
    23 of 25 certainly-enrolled employers (Google, Intel, Lockheed, Deloitte...) carry no tag.
    So stem_opt is reported and RANKED on, never gated on. That also matches the standing rule
    that absence of a federal record is not evidence of non-sponsorship (core.VISA_ABSENCE_NOTE,
    docs/INDEX.md).
    """
    counts = core.load_sponsor_counts() or {}
    visa = core.load_visa_tags() or {}
    known, _urls = feb._known_sources()        # SOURCES *and* the boards table -- see its docstring
    # Pass the boards table in too. SOURCES alone leaves every ADOPTED board invisible to the
    # prefix match, so "COGNIZANT TECHNOLOGY SOLUTIONS US" and "DELOITTE CONSULTING LLP" read
    # as unknown when both are already scraped and carry 73 and 302 live jobs.
    try:
        _adopted = [c for _u, _a, c in scraper.custom_sources()]
    except Exception:
        _adopted = []
    match_source = ce.build_sources_matcher(_adopted)  # which known name, for the report

    # The boards-table half on its own, so a row can say WHERE we already have it. A company
    # adopted by a past probe lives only here, and reading it as unknown is the exact bug
    # _known_sources() was written to stop.
    try:
        table = {scraper._norm_name(c or "") for _u, _a, c in scraper.custom_sources()}
    except Exception:
        table = set()

    agg = collections.OrderedDict()
    dropped = collections.Counter()
    for r in records:
        name = (r.get("company") or "").strip()
        key = scraper._norm_name(name)
        if not key:
            continue
        if unusable_name(name):
            # Named, not silent: an aggregator producing these is an input-quality signal, and
            # a count that only ever appears in a variable is a count nobody reads.
            dropped[name] += 1
            continue
        slot = agg.get(key)
        if slot is None:
            slot = agg[key] = {"company": name, "channels": set(), "seen": 0, "kept": 0,
                               "sample": "", "phrases": collections.Counter()}
        slot["channels"].add(r.get("channel") or "")
        # EVERY posting, not just the ones title_verdict keeps. The question this column answers
        # is "which query surfaced this employer"; gating it on the title filter would conflate
        # query quality with filter quality AND break the sum-to-postings_seen property above.
        phrase = (r.get("phrase") or "").strip()
        if phrase:
            slot["phrases"][phrase] += 1
        slot["seen"] += 1
        title = (r.get("title") or "").strip()
        if title and scraper.title_verdict(title)[0]:
            slot["kept"] += 1
            if not slot["sample"]:
                slot["sample"] = title

    if dropped:
        print("dropped %d posting(s) with an unidentifiable employer name: %s"
              % (sum(dropped.values()),
                 ", ".join("%s x%d" % (n, c) for n, c in dropped.most_common(8))))

    rows = []
    for key, s in agg.items():
        name = s["company"]
        n, method = scraper._safe_sponsor_match(name, counts)
        tags = core.visa_tags(name, visa)
        rows.append({
            "company": name,
            "channel": ",".join(sorted(c for c in s["channels"] if c)),
            "harvest_phrases": _fmt_phrases(s["phrases"]),
            "postings_seen": s["seen"],
            "pm_titles_kept": s["kept"],
            "sample_title": s["sample"],
            # THREE matchers, because each alone leaves a hole and the cost of a miss is a
            # wasted probe slot on a board we already scrape. _known_sources() is exact (plus
            # a de-spaced variant), so it answers "no" for every federal spelling that is
            # longer than our own: _norm_name("META PLATFORMS INC") is "meta platforms" and
            # SOURCES holds "meta". build_sources_matcher() IS able to see that -- it is a
            # whole-word prefix match, longest wins, and it already refuses token containment
            # (which had paired "IT AMERICA INC" with "Samsung Research America"). Until
            # 2026-08-31 its answer only reached the REPORT column while the FLAG that
            # probe_discovered.load_candidates() filters on ignored it, so the two disagreed
            # by construction. Measured on a 1,971-row ranked-sponsor screen: 125 of the
            # 1,674 rows queued for probing were already scraped -- Amazon, Meta, Oracle,
            # JPMorgan Chase, Comcast, Rivian. 7% of the queue, every run.
            "in_sources": "yes" if (key in known or key.replace(" ", "") in known
                                    or match_source(name)) else "no",
            "sources_name": match_source(name) or "",
            "in_boards_table": "yes" if key in table else "no",
            "h1b_filings": n,
            "match_method": method,
            "visa_tags": "|".join(tags),
            "stem_opt": "yes" if "stem_opt" in tags else "no",
            "cap_exempt": "yes" if core.is_cap_exempt(name) else "no",
            "bodyshop": "yes" if core.BODYSHOP_RE.search(name) else "no",
        })

    # Rank the probe queue: sponsor evidence first, then how hard the employer is hiring. This
    # is an ordering, not a filter -- every harvested company stays in the file.
    #
    # "employer_loose" sorts BELOW an equally-large exact match, because a posting feed gives us
    # no headcount and that is the input _safe_sponsor_match needs to adjudicate the loose
    # normaliser. Measured: a fake "Acme Co" inherits "acme"'s filings because _LEGAL_SUFFIX
    # strips "co", and the same mechanism is what lets "Target Labs INC" claim Target Corp's
    # 1,411 -- there it is caught by the nine-person ceiling, here there is no ceiling to check.
    # So a loose match still ranks (it is usually right: "Ford Motor Company" is stored as
    # "ford motor"), just never ahead of one we can stand behind.
    rows.sort(key=lambda r: (r["match_method"] == "employer_loose",
                             -(r["h1b_filings"] or 0),
                             r["stem_opt"] != "yes",
                             -(r["pm_titles_kept"] or 0),
                             -(r["postings_seen"] or 0),
                             r["company"].lower()))
    return rows


def write_csv(rows, path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(FIELDNAMES))
        w.writeheader()
        for r in rows:
            w.writerow(r)


def summarise(records, rows):
    new = [r for r in rows if r["in_sources"] == "no"]
    print("\npostings harvested   : %d" % len(records))
    print("distinct employers   : %d" % len(rows))
    print("already a source     : %d" % (len(rows) - len(new)))
    print("  of those, board-table-only: %d"
          % sum(1 for r in rows if r["in_sources"] == "yes" and r["in_boards_table"] == "yes"))
    print("NET-NEW employers    : %d" % len(new))
    if not new:
        return
    print("  with H-1B filings  : %d" % sum(1 for r in new if r["h1b_filings"]))
    print("  with stem_opt tag  : %d" % sum(1 for r in new if r["stem_opt"] == "yes"))
    print("  cap-exempt         : %d" % sum(1 for r in new if r["cap_exempt"] == "yes"))
    print("  body-shop shaped   : %d" % sum(1 for r in new if r["bodyshop"] == "yes"))
    print("  no federal record  : %d   <- rank them low, do NOT drop them (see screen())"
          % sum(1 for r in new if not r["h1b_filings"] and r["stem_opt"] == "no"))
    print("\ntop net-new by sponsor evidence then PM volume:")
    print("  %-38s %6s %5s %4s %s" % ("company", "h1b", "stem", "pm", "sample title"))
    for r in new[:20]:
        print("  %-38.38s %6d %5s %4d %.40s"
              % (r["company"], r["h1b_filings"], r["stem_opt"], r["pm_titles_kept"],
                 r["sample_title"]))


def phrase_stats(rows):
    """Per harvest phrase: postings, employers, net-new, and how many ONLY that phrase found.

    Works off the CSV ROWS, not the raw records, so a report generated from the written file
    gets the same numbers this run printed -- one implementation, no drift.

    net_new DOUBLE-COUNTS on purpose: an employer surfaced by five phrases is net-new for all
    five, so that column sums to more than the run's net-new total and must never be added up.
    `only` is the column that can be: it is a partition of the net-new set (the employers that
    exactly one phrase found), so it sums, and it is the number that decides whether a phrase
    earns its slot next run. A phrase with 300 net_new and 0 only bought nothing the others
    did not already bring in.
    """
    def _int(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    agg = {}
    for r in rows:
        parsed = parse_phrases(r.get("harvest_phrases"))
        solo = len(parsed) == 1
        new = (r.get("in_sources") or "").strip().lower() != "yes"
        for phrase, n in parsed.items():
            d = agg.setdefault(phrase, {"phrase": phrase, "postings": 0, "employers": 0,
                                        "net_new": 0, "only": 0, "h1b": 0, "stem_opt": 0,
                                        "cap_exempt": 0})
            d["postings"] += n
            d["employers"] += 1
            if not new:
                continue
            d["net_new"] += 1
            d["only"] += 1 if solo else 0
            d["h1b"] += 1 if _int(r.get("h1b_filings")) else 0
            d["stem_opt"] += 1 if (r.get("stem_opt") or "").strip().lower() == "yes" else 0
            d["cap_exempt"] += 1 if (r.get("cap_exempt") or "").strip().lower() == "yes" else 0
    return sorted(agg.values(), key=lambda d: (-d["only"], -d["net_new"], d["phrase"]))


def print_phrase_breakdown(stats, asked=()):
    """What each ROLE bought. Silent when the run had no phrases at all (--channel csv)."""
    if not stats:
        return
    print("\nper-role: what each harvest phrase surfaced")
    print("  net-new double-counts (an employer found by 5 phrases is net-new for all 5);")
    print("  `only` is that phrase's marginal contribution, and it is the column that sums.")
    print("  %-32s %8s %6s %7s %5s %5s %5s %4s"
          % ("phrase", "postings", "empl", "net-new", "only", "h1b", "stem", "cap"))
    for d in stats:
        print("  %-32.32s %8d %6d %7d %5d %5d %5d %4d"
              % (d["phrase"], d["postings"], d["employers"], d["net_new"], d["only"],
                 d["h1b"], d["stem_opt"], d["cap_exempt"]))
    # A phrase LinkedIn refused and a phrase nobody is hiring for both produce no row above, and
    # dropping a good phrase because it got 429'd is the expensive version of that confusion.
    # Same reasoning as the 0-postings note in main() and scraper.scrape_jobspy's own.
    quiet = [p for p in asked if p not in {d["phrase"] for d in stats}]
    if quiet:
        print("  returned NOTHING (blocked, or genuinely nothing new?): %s" % ", ".join(quiet))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="linkedin",
                    help="comma-separated: %s, or 'csv'" % ",".join(JOBSPY_SITES))
    ap.add_argument("--in", dest="infile", default="",
                    help="for --channel csv: a name-per-line or CSV file")
    ap.add_argument("--phrases", default="|".join(PM_PHRASES),
                    help="pipe-separated harvest phrases, or 'all' for ALL_PHRASES (%d)"
                         % len(ALL_PHRASES))
    ap.add_argument("--location", default=scraper.JOBSPY_LOCATION)
    ap.add_argument("--hours", type=int, default=24, help="posting age window (24 or 168)")
    ap.add_argument("--results", type=int, default=100, help="results_wanted per query")
    ap.add_argument("--limit", type=int, default=0, help="cap harvested postings (smoke test)")
    ap.add_argument("--out", default=REPORT)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    channels = [c.strip().lower() for c in a.channel.replace(",", " ").split() if c.strip()]
    # 'all' rather than 24 quoted phrases on one command line: the shell quoting is a real
    # error source, and a run whose inputs live only in shell history cannot be reproduced.
    phrases = (list(ALL_PHRASES) if a.phrases.strip().lower() == "all"
               else [p.strip() for p in a.phrases.split("|") if p.strip()])
    if not channels:
        raise SystemExit("nothing to harvest")

    records = []
    if "csv" in channels:
        if not a.infile:
            raise SystemExit("--channel csv needs --in <file>")
        records += harvest_csv(a.infile)
        channels = [c for c in channels if c != "csv"]
    if channels:
        bad = [c for c in channels if c not in JOBSPY_SITES]
        if bad:
            raise SystemExit("unknown channel(s): %s" % ", ".join(bad))
        if not phrases:
            raise SystemExit("no phrases to search")
        print("harvesting %d channel(s) x %d phrase(s), %dh window, %d results each"
              % (len(channels), len(phrases), a.hours, a.results))
        records += harvest_jobspy(channels, phrases, a.location, a.hours, a.results,
                                  verbose=a.verbose)

    if a.limit:
        records = records[:a.limit]
    if not records:
        # A silent zero and a genuinely quiet day look identical otherwise, and for LinkedIn the
        # overwhelmingly likely cause is a 429 -- say so rather than "no new companies".
        print("\n0 postings harvested. For an aggregator channel that usually means the site "
              "refused the request (rate limit or datacenter IP), not that nobody is hiring.")
        return

    rows = screen(records)
    write_csv(rows, a.out)
    summarise(records, rows)
    print_phrase_breakdown(phrase_stats(rows), asked=phrases)
    print("\nwrote %s (%d rows)" % (a.out, len(rows)))
    print("next: python scripts/probe_discovered.py --csv %s" % a.out)


if __name__ == "__main__":
    main()
