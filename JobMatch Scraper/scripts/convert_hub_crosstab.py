#!/usr/bin/env python3
"""
convert_hub_crosstab.py — turn a USCIS H-1B Employer Data Hub *crosstab export* into the
per-fiscal-year CSVs that scraper/build_sponsor_counts.py already knows how to read.

WHY THIS EXISTS. The Hub used to publish one static CSV per fiscal year at
    https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-<FY>.csv
Those still resolve, but only through **FY2023** — FY2024 and later 404. Worse, the FY2023 file
carries Last-Modified 31 May 2023, four months before FY2023 closed: it is a partial-year
snapshot holding 33,332 rows against the 57,415 the Hub reports today. Anything built on it
understates every employer and draws a ~70% cliff on the last bar of the company chart.

The only current source is the Tableau viz behind the Hub page, exported via
"Crosstab View -> Download to Excel -> CSV". That export is NOT shaped like the static files:

  1. UTF-16LE with a BOM, not UTF-8.
  2. TAB-delimited, despite the .csv name.
  3. Different column names — "Employer (Petitioner) Name", not "Employer"; and the fiscal-year
     header carries trailing spaces ("Fiscal Year   ").
  4. Approvals are split six ways (New Employment / Continuation / Change with Same Employer /
     New Concurrent / Change of Employer / Amended) instead of Initial + Continuing.
  5. Numbers may carry thousands separators.
  6. A missing employer is the literal string "Null", where the static files leave it blank.
     build_sponsor_counts skips blank employers; left alone, "Null" becomes an employer with
     tens of thousands of approvals and outranks Amazon.

Every one of those is a silent failure in build_sponsor_counts.py: a wrong header name prints
one skippable warning and contributes nothing, and a thousands separator raises ValueError
inside a bare `except` that leaves n=0 — which zeroes exactly the LARGE employers while small
ones parse fine. So the conversion is done here, once, rather than by widening that reader.

MAPPING, and how it was checked. Initial <- New Employment; Continuing <- the other five.
Verified against the last fiscal year where a complete static file and the crosstab overlap:

    FY2020        crosstab     static     delta
    Initial        121,874    122,894    -1,020
    Continuing     304,841    303,830    +1,011
    TOTAL          426,715    426,724        -9

i.e. the total agrees to 0.002%, and the ~1,020 that moves between the two buckets is a
category reclassification (change-of-employer petitions counted as Initial in the old schema).
build_sponsor_counts sums Initial + Continuing, so that reclassification cannot affect it.

    python scripts/convert_hub_crosstab.py "Employer Information.csv" \
           --out "../USCIS H-1B Data Hub/raw_csv"

Refuses to write a fiscal year that looks truncated — see MIN_ROWS / MIN_APPROVALS. That guard
is the whole point: it is what would have caught the FY2023 file three months ago.
"""
import argparse
import collections
import csv
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# The static-file header, reproduced exactly. build_sponsor_counts.py matches "employer" and
# "fiscal year" as EXACT lowercased keys and prefix-matches the approval columns, so these
# spellings are load-bearing — do not "tidy" them.
OUT_COLS = ["Fiscal Year", "Employer",
            "Initial Approval", "Initial Denial",
            "Continuing Approval", "Continuing Denial",
            "NAICS", "Tax ID", "State", "City", "ZIP"]

INITIAL = ["New Employment"]
CONTINUING = ["Continuation", "Change with Same Employer", "New Concurrent",
              "Change of Employer", "Amended"]

# A complete Hub fiscal year has run 48k-55k employer rows and 380k-480k approvals every year
# since FY2020. Roughly half the smallest observed year is a floor a real year cannot cross and
# a mid-year snapshot cannot clear.
MIN_ROWS = 24000
MIN_APPROVALS = 190000

NULLISH = {"", "null", "none", "(null)", "%null%"}


def _num(v):
    """Int from a cell that may be blank, '1,788', or '1788.0'."""
    s = str(v or "").replace(",", "").strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _find(fieldnames, *want):
    """The real header whose stripped, case-folded name matches any of `want`."""
    norm = {(c or "").strip().lower(): c for c in fieldnames}
    for w in want:
        if w.lower() in norm:
            return norm[w.lower()]
    return None


def read_crosstab(path):
    """Yield rows in the static-file shape from the UTF-16 tab-delimited export."""
    # utf-16 (not utf-16-le) so Python consumes the BOM and picks the byte order itself.
    with open(path, encoding="utf-16", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fn = reader.fieldnames or []
        emp_c = _find(fn, "Employer (Petitioner) Name", "Employer")
        fy_c = _find(fn, "Fiscal Year")
        if not (emp_c and fy_c):
            sys.exit("  ! %s: expected an employer and a fiscal-year column, got %s"
                     % (os.path.basename(path), ", ".join(repr(c) for c in fn[:6])))
        cols = {
            "NAICS":  _find(fn, "Industry (NAICS) Code", "NAICS"),
            "Tax ID": _find(fn, "Tax ID"),
            "City":   _find(fn, "Petitioner City", "City"),
            "State":  _find(fn, "Petitioner State", "State"),
            "ZIP":    _find(fn, "Petitioner Zip Code", "ZIP"),
        }
        pairs = []
        for group, names in (("i", INITIAL), ("c", CONTINUING)):
            for base in names:
                pairs.append((group, _find(fn, base + " Approval"), _find(fn, base + " Denial")))

        for row in reader:
            emp = (row.get(emp_c) or "").strip()
            # "Null" is this export's way of saying blank. Pass it through as blank so
            # build_sponsor_counts' own blank-employer skip catches it.
            if emp.lower() in NULLISH:
                emp = ""
            ia = idn = ca = cd = 0
            for group, ac, dc in pairs:
                a = _num(row.get(ac)) if ac else 0
                d = _num(row.get(dc)) if dc else 0
                if group == "i":
                    ia += a
                    idn += d
                else:
                    ca += a
                    cd += d
            out = {"Fiscal Year": _num(row.get(fy_c)), "Employer": emp,
                   "Initial Approval": ia, "Initial Denial": idn,
                   "Continuing Approval": ca, "Continuing Denial": cd}
            for key in ("NAICS", "Tax ID", "State", "City", "ZIP"):
                val = (row.get(cols[key]) or "").strip() if cols[key] else ""
                out[key] = "" if val.lower() in NULLISH else val
            yield out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("crosstab", nargs="+", help="Employer Information.csv from the Tableau export")
    ap.add_argument("--out", default=os.path.join("..", "USCIS H-1B Data Hub", "raw_csv"),
                    help="directory to write h1b_<FY>.csv into (created if absent)")
    ap.add_argument("--force", action="store_true",
                    help="write fiscal years that fail the completeness floor anyway")
    args = ap.parse_args()

    rows = collections.defaultdict(list)
    for path in args.crosstab:
        print("reading %s ..." % path)
        n = 0
        for row in read_crosstab(path):
            if not row["Fiscal Year"]:
                continue
            rows[row["Fiscal Year"]].append(row)
            n += 1
        print("  %s rows" % format(n, ","))

    if not rows:
        sys.exit("  ! nothing to write — no rows carried a fiscal year")

    os.makedirs(args.out, exist_ok=True)
    print("\n%-8s %10s %12s %10s   %s" % ("FY", "rows", "approvals", "employers", "verdict"))
    written, skipped = 0, []
    for fy in sorted(rows):
        rs = rows[fy]
        appr = sum(r["Initial Approval"] + r["Continuing Approval"] for r in rs)
        emps = len({r["Employer"] for r in rs if r["Employer"]})
        short = len(rs) < MIN_ROWS or appr < MIN_APPROVALS
        verdict = "OK"
        if short:
            verdict = "PARTIAL - skipped (--force to write anyway)"
            if not args.force:
                skipped.append(fy)
        print("%-8s %10s %12s %10s   %s"
              % ("FY%d" % fy, format(len(rs), ","), format(appr, ","), format(emps, ","), verdict))
        if short and not args.force:
            continue
        path = os.path.join(args.out, "h1b_%d.csv" % fy)
        # UTF-8 with NO BOM: build_sponsor_counts opens with encoding="utf-8", and a BOM would
        # ride into the first header name so "fiscal year" would no longer match.
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=OUT_COLS)
            w.writeheader()
            w.writerows(rs)
        written += 1

    print("\nwrote %d file(s) to %s" % (written, os.path.abspath(args.out)))
    if skipped:
        print("SKIPPED as partial: %s" % ", ".join("FY%d" % y for y in skipped))
        print("A partial year in the tier window understates every employer. Re-export it.")


if __name__ == "__main__":
    main()
