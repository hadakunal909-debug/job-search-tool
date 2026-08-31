#!/usr/bin/env python3
"""
build_sponsor_counts.py — turn the USCIS H-1B Data Hub CSVs into sponsor_counts.json
-----------------------------------------------------------------------------------
`core.sponsor_strength(company, counts)` already tiers an employer by H-1B filing
VOLUME (high >=1000 / medium >=100 / low >=1) and the feed already renders that tier
next to the H1B badge. It just never fires, because `core.load_sponsor_counts()` reads
a `sponsor_counts.json` that has never been built. This builds it.

Input: the USCIS H-1B Employer Data Hub bulk CSVs (one per fiscal year), columns
    "Fiscal Year",Employer,"Initial Approval","Initial Denial",
    "Continuing Approval","Continuing Denial",NAICS,"Tax ID",State,City,ZIP

Run from the app directory:
    python -m scraper.build_sponsor_counts
    python -m scraper.build_sponsor_counts "path/to/raw_csv_fy2009-2023" --years 2019-2023

Writes, next to sponsors.txt:
    sponsor_counts.json           {normalized_name: approvals}   every USCIS spelling
    sponsor_years.json            {normalized_name: {fy: approvals}}   our companies only

    The second one drives the company panel's year-by-year history. It is restricted to names
    in our universe because the full per-year map over ~118k USCIS spellings is tens of MB,
    and the panel can only ever be opened for an employer that is in our corpus. Its totals
    reconcile with sponsor_counts.json over the same window, since both are summed from one
    pass of the same rows.

NO PER-STATE FILE, AND DON'T ADD ONE
    The Hub's State/City columns are the PETITIONER's mailing address, not the worksite.
    Aggregating FY2019-23 by state gives Google 100% CA, Microsoft 100% WA, Infosys 100% TX
    and Deloitte 86% PA (its Hermitage processing centre) — so a "sponsors N H-1Bs in your
    state" badge would tell a student in Boston the opposite of the truth about where those
    employers hire. Worksite-level data lives in the DOL LCA disclosure files, which carry a
    real WORKSITE_STATE; see build_sponsors.py for that source.

WHY THE NAME RESOLUTION STEP EXISTS
    USCIS writes "AMAZON COM SERVICES LLC"; our job rows say "Amazon". Both go through
    scraper._norm_name, giving "amazon com services" vs "amazon" — and sponsor_strength
    does an EXACT dict lookup, so the count would never be found. So after aggregating
    by the USCIS spelling we also resolve every company in OUR universe (the distinct
    companies in the jobs DB + sponsors.txt) to its USCIS entries and emit the total
    under the key our runtime lookup will actually use.

    Resolution is deliberately conservative — exact normalized match, else our whole
    normalized name must be a leading TOKEN PREFIX of the USCIS name ("amazon" matches
    "amazon com services", never "pan amazon"). No fuzzy matching: a wrong merge here
    would overstate a sponsor's track record, which is the one number a student would
    act on. Names that normalize to a single short or generic token are skipped rather
    than guessed at (this is the known "US Bank" -> "bank" trap).
"""
import os
import re
import sys
import csv
import json
import argparse
import collections
import datetime

if hasattr(sys.stdout, "reconfigure"):      # absent under Passenger / some cron wrappers
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scraper import _norm_name                       # the exact key sponsor_strength looks up

OUT = "sponsor_counts.json"
# {our_normalized_name: {fiscal_year: approvals}} — the history behind the company panel's
# year-by-year chart. Separate from OUT because it covers only the names we can actually be
# asked about, while OUT keeps every USCIS spelling so the tier lookup can fall back on one.
OUT_YEARS = "sponsor_years.json"

# "raw_csv" FIRST, and it has no fiscal-year span in its name on purpose. The original folder
# was called raw_csv_fy2009-2023, which meant every refresh that added a year was a code edit
# here. Worse, that folder's h1b_2023.csv is a PARTIAL year (33,332 rows against the 57,415 the
# Hub reports today — USCIS published it in May 2023, four months before FY2023 closed), so a
# fallback that silently found it would understate every employer. It stays in the list only so
# an unconverted checkout still builds something, and it stays on DISK because the sibling
# "USCIS H-1B Data Hub/majors_with_careers.py" globs that literal name and would silently
# return zero files if it were renamed. Populate raw_csv/ with scripts/convert_hub_crosstab.py.
DEFAULT_DIRS = [
    os.path.join("..", "USCIS H-1B Data Hub", "raw_csv"),
    os.path.join("USCIS H-1B Data Hub", "raw_csv"),
    os.path.join("..", "USCIS H-1B Data Hub", "raw_csv_fy2009-2023"),
    os.path.join("USCIS H-1B Data Hub", "raw_csv_fy2009-2023"),
]

# Brands whose USCIS filing name shares NO prefix with the name we display, so neither an
# exact nor a prefix match can find them. Keys and values are already _norm_name'd. Kept
# deliberately short and hand-verified against the data — a wrong entry here would credit
# one employer with another's track record, and that number is one a student would act on.
ALIASES = {
    "ey": ("ernst young",),
    "pwc": ("pricewaterhousecoopers",),
    "amd": ("advanced micro devices",),
    "cvs health": ("cvs pharmacy",),
    "meta": ("facebook",),
    "alphabet": ("google",),
    "tcs": ("tata consultancy",),
    "j j": ("johnson johnson",),           # "Johnson & Johnson" normalizes to "j j"
    "ibm": ("international business machines",),
    "ge": ("general electric",),
    "hp": ("hewlett packard",),
    "gm": ("general motors",),
    "p g": ("procter gamble",),
    "3m": ("3m",),
    "rtx": ("raytheon",),
    "bms": ("bristol myers squibb",),
    "aws": ("amazon web services",),
    "amat": ("applied materials",),         # the scraper labels Applied Materials "Amat"
    "hcl": ("hcl america",),                # "HCL Technologies" -> "hcl" once the suffix is stripped
    "exl service": ("exlservice",),
    "instacart": ("maplebear",),            # files as MAPLEBEAR INC DBA INSTACART
    # Both of these were WRONG ON THE LIVE FEED until 2026-08-31, and neither was a refresh
    # regression — they predate it. Found by scanning corpus employers whose tier was low or
    # blank while a de-spaced variant of their name held thousands of approvals.
    #   Walmart files as "WAL-MART ASSOCIATES INC" -> "wal mart associates". Our card says
    #   "Walmart" -> "walmart", which is not a token prefix of it, so 817 live job cards showed
    #   ('low', 7) for a 13,791-approval employer -- the wrong sponsorship COLOUR, on the one
    #   number a student would act on.
    "walmart": ("wal mart",),
    #   Same shape, opposite spelling: the board writes "JPMorganChase" as one word, USCIS
    #   files as "JPMORGAN CHASE & CO". 37 live jobs, tier was blank.
    "jpmorganchase": ("jpmorgan chase",),
    "supermicro": ("super micro computer",),    # board says "Supermicro", files as "Super Micro"
    "spacex": ("space exploration",),           # Space Exploration Technologies Corp
    # REJECTED while compiling the four above, and listed so nobody "helpfully" adds them: the
    # same scan proposed WM -> "w m rice university" (Waste Management onto RICE UNIVERSITY) and
    # BD -> "bdo" (Becton Dickinson onto the accounting firm). Two-letter names are the "US Bank
    # -> bank" trap; resolve() already refuses them as too generic, and it is right to.
}

# A company name that normalizes to one of these (or to fewer than 4 characters) is too
# generic to expand by prefix — "bank" would swallow every bank in the country.
GENERIC = {
    "bank", "health", "medical", "university", "college", "hospital", "systems", "system",
    "solutions", "services", "consulting", "software", "data", "digital", "global", "national",
    "american", "united", "first", "general", "state", "city", "county", "school", "research",
    "capital", "financial", "insurance", "energy", "media", "network", "networks", "partners",
    "associates", "enterprises", "international", "industries", "science", "sciences", "care",
    # GIVEN NAMES AND PLACE WORDS. Everything above is a generic BUSINESS word; these are words
    # that many unrelated small businesses happen to START with, which the prefix expansion then
    # pools into one number. Both were caught in 2026-08-31's --check as newly "prominent":
    #   "David"   -- the protein-bar startup, board job-boards.greenhouse.io/david. Prefix
    #                expansion credited it with 135 approvals pooled from David Yurman, David
    #                Oppenheimer, David Evans & Associates and nine other unrelated businesses.
    #   "Coastal" -- Coastal Community Bank (jobs.ashbyhq.com/coastal), credited with 111
    #                including Coastal Carolina University's 56.
    # Pooling is RIGHT for the other 96 of 98 single-token names measured (Amazon over
    # "amazon web services", Deloitte over "deloitte tax"), so the matcher is not the problem
    # and must not be loosened or rewritten -- the deny list is the correct instrument.
    "david", "coastal",
    # Same class, found once the companies.json roster joined our_universe(): "Horizon" had
    # 679 approvals pooled from Horizon International Trade, Horizon Therapeutics, Horizon
    # Media, Horizon Softech and Horizon Science Academy -- five unrelated businesses.
    "horizon",
}


def parse_years(spec):
    """'2019-2023' or '2021,2022' -> a set of ints."""
    out = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def _col(cols, prefix):
    """First column whose lowercased name starts with `prefix` — the Hub renamed
    "Initial Approvals" to "Initial Approval" between FY2019 and FY2020."""
    for low, orig in cols.items():
        if low.startswith(prefix):
            return orig
    return None


def read_hub_csvs(directory, years):
    """Aggregate approvals per USCIS employer spelling.

    Returns (totals, by_year, seen_years), where `totals` covers only the `years` window (it
    feeds the tier, whose thresholds were calibrated on FY2019-23) and `by_year` is
    {key: {fy: approvals}} across EVERY fiscal year in the folder. The per-year map is what the
    company panel draws a history from, and a history has to be longer than the window the tier
    happens to use — so every file is read regardless, and `totals` is summed out of `by_year`
    at the end rather than by skipping rows on the way in. Same arithmetic, one pass.

    Counts Initial + Continuing APPROVALS only. Denials are read but not summed: a denial
    is not evidence of willingness to sponsor, and mixing them would inflate the tier.
    """
    by_year = collections.defaultdict(collections.Counter)
    seen_years, files, skipped_blank = set(), 0, 0

    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith(".csv"):
            continue
        path = os.path.join(directory, name)
        files += 1
        with open(path, newline="", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            cols = {(c or "").strip().lower(): c for c in (reader.fieldnames or [])}
            emp_c = cols.get("employer")
            if not emp_c:
                print("  ! %s has no Employer column - skipped" % name)
                continue
            fy_c = cols.get("fiscal year")
            # FY2019 and earlier name these "Initial Approvals" (plural), FY2020+ singular.
            # Prefix-match so one reader handles every year of the Hub export.
            ia_c = _col(cols, "initial approval")
            ca_c = _col(cols, "continuing approval")
            if not (ia_c and ca_c):
                print("  ! %s has no approval columns (%s) - skipped"
                      % (name, ", ".join(sorted(cols))[:120]))
                continue

            # Fall back to the year in the filename (h1b_2019.csv) when a row's own Fiscal Year
            # cell is blank or unparseable, so a stray row can't land in an "unknown" bucket.
            fm = re.search(r"(20\d\d)", name)
            file_fy = int(fm.group(1)) if fm else 0

            for row in reader:
                emp = (row.get(emp_c) or "").strip()
                if not emp:
                    skipped_blank += 1          # the Hub files carry blank-employer rows
                    continue
                fy = 0
                if fy_c:
                    try:
                        fy = int(float(row.get(fy_c) or 0))
                    except ValueError:
                        fy = 0
                fy = fy or file_fy
                n = 0
                for c in (ia_c, ca_c):
                    if not c:
                        continue
                    try:
                        n += int(float(row.get(c) or 0))
                    except ValueError:
                        pass
                if n <= 0:
                    continue
                key = _norm_name(emp)
                if not key:
                    continue
                if fy:
                    seen_years.add(fy)
                    by_year[key][fy] += n

        print("  read %-16s (%d employers so far)" % (name, len(by_year)))

    print("  %d file(s); %d blank-employer rows skipped" % (files, skipped_blank))
    # The tier window, summed out of the full history so both numbers come from one pass.
    totals = collections.Counter()
    for key, yrs in by_year.items():
        t = sum(n for fy, n in yrs.items() if not years or fy in years)
        if t:
            totals[key] = t
    return totals, by_year, seen_years


def our_universe():
    """Distinct company names the app actually shows: the jobs table + every configured
    board + sponsors.txt. Degrades to whatever of those is reachable.

    SOURCES matters as much as the jobs table. A resolved entry is only written for a name
    that appears HERE, so a company with no live postings on the day of the rebuild loses
    its count entirely — and that is not hypothetical: the 30-day purge emptied several
    employers, and the next rebuild silently dropped Meta from ~26,700 approvals to 1,
    along with Mastech Digital and Northwestern Mutual. A configured board is a permanent
    part of our universe whether or not it happens to be hiring today.
    """
    names = set()
    try:
        import db
        for r in db.load_jobs(cols=db.COLS_COMPANY):   # names only: a bare load_jobs() is ~130 MB
            c = (r.get("company") or "").strip()
            if c:
                names.add(c)
        print("  %d distinct companies from the jobs table" % len(names))
    except Exception as e:
        print("  (jobs table unavailable: %s)" % str(e)[:90])
    try:
        import scraper
        before = len(names)
        for _u, _a, c in list(scraper.SOURCES) + list(scraper.custom_sources()):
            if c and c.strip():
                names.add(c.strip())
        print("  +%d from configured boards (SOURCES)" % (len(names) - before))
    except Exception as e:
        print("  (SOURCES unavailable: %s)" % str(e)[:90])
    if os.path.exists("sponsors.txt"):
        with open("sponsors.txt", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    names.add(ln)
    # companies.json is the DURABLE ROSTER -- it carries every employer the directory has
    # ever listed, not just the ones hiring today (scripts/build_companies.py::build(carry)).
    # Without it this function reproduces the exact bug its own docstring describes, one step
    # further out: /companies keeps showing Sony, Zoom and Broadridge because the roster kept
    # them, but their H-1B number reads 0 because no resolved key was written for a name that
    # was not in this set. Sony files as "SONY INTERACTIVE ENTERTAINMENT" (772 approvals) and
    # nothing pools it onto "sony" unless "Sony" is asked about here.
    try:
        before = len(names)
        with open("companies.json", encoding="utf-8") as f:
            for row in (json.load(f) or {}).get("rows") or []:
                if isinstance(row, list) and row and str(row[0]).strip():
                    names.add(str(row[0]).strip())
        print("  +%d from the companies.json roster" % (len(names) - before))
    except Exception as e:
        print("  (companies.json unavailable: %s)" % str(e)[:90])
    print("  %d names in our universe (jobs + boards + sponsors.txt + roster)" % len(names))
    return names


def resolve(names, totals, by_year=None):
    """Map our company spellings onto the USCIS aggregates.

    For each of our names we gather EVERY USCIS entry that belongs to it and sum them:
      - the exact normalized key, when USCIS also files under the short brand name;
      - an explicit ALIASES entry, for brands whose legal filing name shares no prefix
        with the brand ("EY" files as ERNST & YOUNG);
      - token-prefix expansion, when the name is distinctive enough to be safe
        ("amazon" <- "amazon com services", "amazon web services", ...).

    Summing all of them matters: "amazon" exists as its own USCIS entry with ONE
    approval (some unrelated small filer), while Amazon's real 60k sit under the longer
    spellings. Taking the exact match alone would tier Amazon "low".
    """
    prefix_index = collections.defaultdict(list)
    for key in totals:
        prefix_index[key.split(" ", 1)[0]].append(key)

    def expand(name):
        """USCIS keys that are `name` or begin with `name` at a token boundary."""
        head = name.split(" ", 1)[0]
        return [k for k in prefix_index.get(head, ()) if k == name or k.startswith(name + " ")]

    extra, extra_years = {}, {}
    report = collections.Counter()
    examples = collections.defaultdict(list)

    for raw in sorted(names):
        key = _norm_name(raw)
        if not key:
            report["blank"] += 1
            continue

        matches, how = set(), []
        if key in totals:
            matches.add(key)
            how.append("exact")
        for alias in ALIASES.get(key, ()):
            hits = expand(alias)
            if hits:
                matches.update(hits)
                how.append("alias:" + alias)
        # Prefix expansion needs a distinctive stem — "bank" or "amd" would drag in
        # unrelated filers, so those rely on an exact match or an alias instead.
        generic = len(key) < 4 or key in GENERIC
        if not generic:
            hits = expand(key)
            # Drop the long tail of unrelated filers that merely share the brand's first
            # token. Measured on the live index: "ge" prefix-reaches 30 keys of which 26 are
            # one-off firms (GE Capital, GE Aviation Systems...), "hp" reaches 11 of which 10
            # are ("HP Buildings", "HP & Assoc PC"), and "meta" picks up Meta Hub IT
            # Solutions. A key contributing under a thousandth of the brand's best single
            # match is not that brand, and summing them inflates the number the tooltip shows.
            if hits:
                best = max(totals[k] for k in hits)
                floor = max(2, best // 1000)
                keep = [k for k in hits if totals[k] >= floor]
                dropped = len(hits) - len(keep)
                if keep:
                    matches.update(keep)
                    how.append("prefix")
                if dropped:
                    report["prefix_tail_dropped"] += dropped

        if not matches:
            report["too_generic" if generic and not how else "miss"] += 1
            bucket = "too_generic" if generic else "miss"
            if len(examples[bucket]) < 10:
                examples[bucket].append("%s -> %s" % (raw, key))
            continue

        total = sum(totals[k] for k in matches)
        extra[key] = max(extra.get(key, 0), total)
        # The same matched USCIS spellings, kept apart by fiscal year. Written ONLY for names in
        # our universe: the full per-year map over all ~118k USCIS spellings would be tens of
        # megabytes, and the panel can only ever ask about an employer that is in our corpus.
        if by_year is not None:
            hist = collections.Counter()
            for k in matches:
                hist.update(by_year.get(k) or {})
            if hist:
                prev = extra_years.get(key)
                if prev is None or sum(hist.values()) > sum(prev.values()):
                    extra_years[key] = dict(hist)

        tag = "+".join(how)
        report[tag if tag in ("exact", "prefix", "exact+prefix") else "alias"] += 1
        if len(examples["resolved"]) < 12 and total >= 500:
            examples["resolved"].append("%-28s -> %-24s %8s  via %d USCIS name(s) [%s]"
                                        % (raw, key, format(total, ","), len(matches), tag))
    return extra, extra_years, report, examples


def main():
    ap = argparse.ArgumentParser(description="Build sponsor_counts.json from the USCIS H-1B Data Hub CSVs.")
    ap.add_argument("directory", nargs="?", default=None, help="folder of h1b_YYYY.csv files")
    ap.add_argument("--years", default="2021-2025",
                    help="fiscal years to count (default 2021-2025 — recent filings predict "
                         "current sponsoring far better than a 15-year sum)")
    # WHY FIVE YEARS, AND WHY THESE FIVE. core.sponsor_strength tiers on ABSOLUTE counts
    # (>=1000 high, >=100 medium), so the tier is a direct function of how WIDE this window is,
    # and nothing downstream renormalizes. Measured on the raw CSVs: narrowing FY2019-2023 to a
    # three-year window cuts the "high" tier from 169 employers to 95. So the invariant to hold
    # across a refresh is the WIDTH, not the specific years — keep it at five.
    #
    # FY2021-2025 rather than a later end: FY2026 is only published through Q3 and a partial
    # year in a SUM depresses each employer by a different fraction (the old FY2023 file was 38%
    # of a normal year nationally but 26% for Amazon). Excluded from the history too, or it
    # draws a false cliff as the last bar of every company chart — which is the exact artifact
    # this refresh existed to remove.
    #
    # FY2021 rather than FY2020 as the start: the pre-FY2020 Hub exports carry no CONSOLIDATED
    # employer row, only small per-TaxID ones, so Microsoft reads 13 in FY2019 and ~7,000 in
    # FY2020. Any window straddling that boundary mixes two different things.
    args = ap.parse_args()

    directory = args.directory
    if not directory:
        directory = next((d for d in DEFAULT_DIRS if os.path.isdir(d)), None)
    if not directory or not os.path.isdir(directory):
        sys.exit("  Could not find the USCIS CSV folder. Pass it explicitly:\n"
                 "    python -m scraper.build_sponsor_counts \"<folder of h1b_YYYY.csv>\"")

    years = parse_years(args.years)
    print("Reading USCIS Hub CSVs from %s (tier window FY %s; history keeps every year)"
          % (directory, args.years))
    totals, by_year, seen = read_hub_csvs(directory, years)
    if not totals:
        sys.exit("  No approvals parsed — check the folder and column names.")
    print("  %d USCIS employer spellings, %s approvals in the window, years seen: %s"
          % (len(totals), format(sum(totals.values()), ","),
             ", ".join(str(y) for y in sorted(seen)) or "n/a"))

    # PER-FISCAL-YEAR CANARY. Print this and read it every time. A complete Hub year has run
    # 380k-480k approvals since FY2020, so a year that lands far below its neighbours is a
    # partial export, not a decline in sponsorship — and it is invisible in the total above.
    # The shipped FY2023 file was 176,950 against FY2022's 466,191 and nothing said so for
    # three months; the whole tier was built on a window with a hole in it.
    per_fy = collections.Counter()
    for yrs in by_year.values():
        for fy, n in yrs.items():
            per_fy[fy] += n
    if per_fy:
        typical = sorted(per_fy.values())[len(per_fy) // 2]
        print("  approvals by fiscal year (in-window years marked *):")
        for fy in sorted(per_fy):
            flag = "*" if not years or fy in years else " "
            warn = "   <-- LOOKS PARTIAL vs the others" if per_fy[fy] < typical * 0.6 else ""
            print("    %s FY%d  %11s%s" % (flag, fy, format(per_fy[fy], ","), warn))

    print("\nResolving our company names onto those aggregates...")
    names = our_universe()
    extra, extra_years, report, examples = resolve(names, totals, by_year)
    print("  resolved: exact %d · prefix %d · exact+prefix %d · via alias %d"
          % (report["exact"], report["prefix"], report["exact+prefix"], report["alias"]))
    print("  unresolved: no filings found %d · too generic to guess %d"
          % (report["miss"], report["too_generic"]))
    for kind, label in (("resolved", "sample of resolved employers (>=500 approvals)"),
                        ("too_generic", "skipped as too generic to match safely"),
                        ("miss", "no USCIS filings found")):
        if examples[kind]:
            print("    %s:" % label)
            for line in examples[kind]:
                print("      %s" % line)

    # PROVENANCE. Until 2026-08-31 neither of these files recorded anything about itself, so
    # the only way to know what fiscal years a shipped sponsor_counts.json covered was to read
    # the --years DEFAULT in this file and hope nobody had passed the flag. That is how the
    # window silently kept a partial FY2023 in it. Same "#meta" convention as visa_tags.json.
    #
    # "years" (the tier window) and "history" (every FY present) are deliberately SEPARATE:
    # read_hub_csvs reads every file and only SUMS the window, so they are different facts, and
    # web.py::sponsor_data_through labels a number that comes from the window.
    #
    # SAFETY: this key is popped in core.load_sponsor_counts / load_sponsor_years, because
    # web.py::sponsor_data_through iterates the VALUES of sponsor_years and int()s their keys
    # inside a bare `except` — a dict of strings there would be swallowed and pin the vintage
    # label at "FY2023" forever. "#" cannot survive _norm_name, so no company lookup can
    # collide with it.
    meta = {
        "built": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": "USCIS H-1B Employer Data Hub",
        "dir": os.path.abspath(directory),
        "files": sorted(f for f in os.listdir(directory) if f.lower().endswith(".csv")),
        "years": sorted(years) if years else sorted(seen),
        "history": sorted(seen),
        "approvals_by_fy": {str(fy): per_fy[fy] for fy in sorted(per_fy)},
    }

    # Our resolved keys go in LAST so they win over any same-key USCIS aggregate.
    out = dict(totals)
    out.update(extra)
    out["#meta"] = dict(meta, keys=len(out) + 1)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), sort_keys=True)

    tiers = collections.Counter()
    for k, n in out.items():
        if k == "#meta":                    # provenance, not an employer — never a tier
            continue
        tiers["high" if n >= 1000 else "medium" if n >= 100 else "low"] += 1
    print("\nWrote %s (%d keys, %.1f MB)."
          % (OUT, len(out) - 1, os.path.getsize(OUT) / 1e6))
    print("Tiers as core.sponsor_strength will read them: high %d · medium %d · low %d"
          % (tiers["high"], tiers["medium"], tiers["low"]))

    # Per-year history, resolved names only — see the note in resolve().
    years_out = dict(extra_years)
    years_out["#meta"] = dict(meta, employers=len(extra_years))
    with open(OUT_YEARS, "w", encoding="utf-8") as f:
        json.dump(years_out, f, separators=(",", ":"), sort_keys=True)
    span = sorted({int(y) for h in extra_years.values() for y in h})
    print("Wrote %s (%d employers, FY%s-%s, %.1f MB)."
          % (OUT_YEARS, len(extra_years), span[0] if span else "?", span[-1] if span else "?",
             os.path.getsize(OUT_YEARS) / 1e6))

    print("\nNOTE: add sponsor_counts.json and sponsor_years.json to the .cpanel.yml copy "
          "list (and scripts/build_deploy_zip.py) or the live feed still won't show tiers.")


if __name__ == "__main__":
    main()
