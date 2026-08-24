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
({company, title, url, location, posted, channel}), so adding or swapping one is a new function
and a --channel value, never a redesign. That matters because no free channel is complete:
LinkedIn rate-limits an unauthenticated IP within a few hundred results, so a run yields low
hundreds of employers, not thousands. Composing channels is the only way to volume.

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

FIELDNAMES = ("company", "channel", "postings_seen", "pm_titles_kept", "sample_title",
              "in_sources", "sources_name", "in_boards_table", "h1b_filings", "match_method",
              "visa_tags", "stem_opt", "cap_exempt", "bodyshop")

# JobSpy sites that are worth asking. Kept here rather than read from JOBSPY_SITES because that
# env var switches the real SCRAPE on, and this script must never depend on it being set.
JOBSPY_SITES = ("linkedin", "indeed", "google", "glassdoor", "zip_recruiter")

# The PM family. Deliberately narrower than scraper.INCLUDE: this is the harvest QUERY, and a
# broader query returns the same employers plus noise. Breadth is applied later instead --
# adopt_everify_boards judges a board with the FULL title filter, so a company found on a PM
# query still counts for the data/AI/SWE roles it also posts.
PM_PHRASES = ("project manager", "product manager", "program manager",
              "technical program manager", "product owner", "scrum master")

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
                            "channel": site})
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
    carries no title, so pm_titles_kept stays 0 for these rows and the probe queue ranks them
    on sponsor evidence alone.
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
                            "url": "", "location": "", "posted": "", "channel": channel})
    else:
        for ln in lines:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append({"company": ln, "title": "", "url": "", "location": "",
                            "posted": "", "channel": channel})
    return out


# ----------------------------------------------------------------------------------- screen

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
    match_source = ce.build_sources_matcher()  # which SOURCES name, for the report column

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
                               "sample": ""}
        slot["channels"].add(r.get("channel") or "")
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
            "postings_seen": s["seen"],
            "pm_titles_kept": s["kept"],
            "sample_title": s["sample"],
            "in_sources": "yes" if (key in known or key.replace(" ", "") in known) else "no",
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="linkedin",
                    help="comma-separated: %s, or 'csv'" % ",".join(JOBSPY_SITES))
    ap.add_argument("--in", dest="infile", default="",
                    help="for --channel csv: a name-per-line or CSV file")
    ap.add_argument("--phrases", default="|".join(PM_PHRASES),
                    help="pipe-separated harvest phrases")
    ap.add_argument("--location", default=scraper.JOBSPY_LOCATION)
    ap.add_argument("--hours", type=int, default=24, help="posting age window (24 or 168)")
    ap.add_argument("--results", type=int, default=100, help="results_wanted per query")
    ap.add_argument("--limit", type=int, default=0, help="cap harvested postings (smoke test)")
    ap.add_argument("--out", default=REPORT)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    channels = [c.strip().lower() for c in a.channel.replace(",", " ").split() if c.strip()]
    phrases = [p.strip() for p in a.phrases.split("|") if p.strip()]
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
    print("\nwrote %s (%d rows)" % (a.out, len(rows)))
    print("next: python scripts/probe_discovered.py --csv %s" % a.out)


if __name__ == "__main__":
    main()
