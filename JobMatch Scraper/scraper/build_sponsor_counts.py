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
    sponsor_counts.json           {normalized_name: approvals}

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

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scraper import _norm_name                       # the exact key sponsor_strength looks up

OUT = "sponsor_counts.json"

DEFAULT_DIRS = [
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
}

# A company name that normalizes to one of these (or to fewer than 4 characters) is too
# generic to expand by prefix — "bank" would swallow every bank in the country.
GENERIC = {
    "bank", "health", "medical", "university", "college", "hospital", "systems", "system",
    "solutions", "services", "consulting", "software", "data", "digital", "global", "national",
    "american", "united", "first", "general", "state", "city", "county", "school", "research",
    "capital", "financial", "insurance", "energy", "media", "network", "networks", "partners",
    "associates", "enterprises", "international", "industries", "science", "sciences", "care",
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
    """Aggregate approvals per USCIS employer spelling. Returns (totals, seen_years).

    Counts Initial + Continuing APPROVALS only. Denials are read but not summed: a denial
    is not evidence of willingness to sponsor, and mixing them would inflate the tier.
    """
    totals = collections.Counter()
    seen_years, files, skipped_blank = set(), 0, 0

    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith(".csv"):
            continue
        m = re.search(r"(20\d\d)", name)
        if m and years and int(m.group(1)) not in years:
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

            for row in reader:
                emp = (row.get(emp_c) or "").strip()
                if not emp:
                    skipped_blank += 1          # the Hub files carry blank-employer rows
                    continue
                if fy_c and years:
                    try:
                        fy = int(float(row.get(fy_c) or 0))
                    except ValueError:
                        fy = 0
                    if fy and fy not in years:
                        continue
                    if fy:
                        seen_years.add(fy)
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
                totals[key] += n

        print("  read %-16s (%d employers so far)" % (name, len(totals)))

    print("  %d file(s); %d blank-employer rows skipped" % (files, skipped_blank))
    return totals, seen_years


def our_universe():
    """Distinct company names the app actually shows: the jobs table + sponsors.txt.
    Degrades to sponsors.txt alone when the DB isn't reachable."""
    names = set()
    try:
        import db
        for r in db.load_jobs():
            c = (r.get("company") or "").strip()
            if c:
                names.add(c)
        print("  %d distinct companies from the jobs table" % len(names))
    except Exception as e:
        print("  (jobs table unavailable: %s)" % str(e)[:90])
    if os.path.exists("sponsors.txt"):
        with open("sponsors.txt", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    names.add(ln)
    print("  %d names in our universe (jobs + sponsors.txt)" % len(names))
    return names


def resolve(names, totals):
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

    extra = {}
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
            if hits:
                matches.update(hits)
                how.append("prefix")

        if not matches:
            report["too_generic" if generic and not how else "miss"] += 1
            bucket = "too_generic" if generic else "miss"
            if len(examples[bucket]) < 10:
                examples[bucket].append("%s -> %s" % (raw, key))
            continue

        total = sum(totals[k] for k in matches)
        extra[key] = max(extra.get(key, 0), total)

        tag = "+".join(how)
        report[tag if tag in ("exact", "prefix", "exact+prefix") else "alias"] += 1
        if len(examples["resolved"]) < 12 and total >= 500:
            examples["resolved"].append("%-28s -> %-24s %8s  via %d USCIS name(s) [%s]"
                                        % (raw, key, format(total, ","), len(matches), tag))
    return extra, report, examples


def main():
    ap = argparse.ArgumentParser(description="Build sponsor_counts.json from the USCIS H-1B Data Hub CSVs.")
    ap.add_argument("directory", nargs="?", default=None, help="folder of h1b_YYYY.csv files")
    ap.add_argument("--years", default="2019-2023",
                    help="fiscal years to count (default 2019-2023 — recent filings predict "
                         "current sponsoring far better than a 15-year sum)")
    args = ap.parse_args()

    directory = args.directory
    if not directory:
        directory = next((d for d in DEFAULT_DIRS if os.path.isdir(d)), None)
    if not directory or not os.path.isdir(directory):
        sys.exit("  Could not find the USCIS CSV folder. Pass it explicitly:\n"
                 "    python -m scraper.build_sponsor_counts \"<folder of h1b_YYYY.csv>\"")

    years = parse_years(args.years)
    print("Reading USCIS Hub CSVs from %s (FY %s)" % (directory, args.years))
    totals, seen = read_hub_csvs(directory, years)
    if not totals:
        sys.exit("  No approvals parsed — check the folder and column names.")
    print("  %d USCIS employer spellings, %s total approvals, years seen: %s"
          % (len(totals), format(sum(totals.values()), ","),
             ", ".join(str(y) for y in sorted(seen)) or "n/a"))

    print("\nResolving our company names onto those aggregates...")
    names = our_universe()
    extra, report, examples = resolve(names, totals)
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

    # Our resolved keys go in LAST so they win over any same-key USCIS aggregate.
    out = dict(totals)
    out.update(extra)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), sort_keys=True)

    tiers = collections.Counter()
    for n in out.values():
        tiers["high" if n >= 1000 else "medium" if n >= 100 else "low"] += 1
    print("\nWrote %s (%d keys, %.1f MB)."
          % (OUT, len(out), os.path.getsize(OUT) / 1e6))
    print("Tiers as core.sponsor_strength will read them: high %d · medium %d · low %d"
          % (tiers["high"], tiers["medium"], tiers["low"]))
    print("\nNOTE: add sponsor_counts.json to the .cpanel.yml copy list or the live "
          "feed still won't show tiers.")


if __name__ == "__main__":
    main()
