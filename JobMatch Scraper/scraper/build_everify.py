#!/usr/bin/env python3
"""
build_everify.py — turn an E-Verify employer snapshot into everify.txt
----------------------------------------------------------------------
The app can flag which employers are enrolled in E-Verify — the signal an F-1
student needs for the STEM-OPT 24-month extension (a STEM-OPT employer MUST use
E-Verify). To power that, `core.load_everify()` reads everify.txt (one company
name per line). This script builds that file from a real E-Verify snapshot.

WHY a snapshot file (not a live lookup): e-verify.gov's employer search returns
403 to non-browser traffic and has no public bulk download. So you download a
real list once and we cross-reference it against the companies WE actually show.

How to use:
  1. Get an E-Verify participating-employers list as .csv or .xlsx. Free option:
     the Kaggle "E-Verify Participating Employers" USCIS export (real data; the
     public copy is ~2018 but large employers' status is stable). Any file with a
     company-name column works.
  2. Run:
         pip install openpyxl        # only if the file is .xlsx
         python -m scraper.build_everify "e_verify_employers.csv"
  3. It keeps only enrolled employers that ALSO match a company we scrape
     (scraper.SOURCES) or a curated large-employer allowlist, SKIPS sketchy
     staffing-mill names, merges with everify.txt, and writes it sorted.

LEGITIMACY GUARD (important): a "E-Verify" badge must never lend credibility to an
OPT/H-1B body-shop. So we only ever write a name that is (a) in our vetted set
(SOURCES companies have a real, hand-verified ATS board — mills almost never do)
or the allowlist, AND (b) not caught by the body-shop name heuristic. Skipped
names are printed so you can review them. The badge is a HINT — the UI tells the
user to confirm current status at e-verify.gov.
"""
import os
import re
import sys
import csv

import scraper

OUT = "everify.txt"

# Status values that mean "currently enrolled" (keep). Anything containing these
# is dropped. The snapshot may not have a status column at all — then we keep all.
_DROP_STATUS = ("terminated", "closed", "inactive", "deactivated")

# Header names we'll accept for the employer-name and status columns (case-insensitive).
_NAME_HEADERS = ("employer", "employer name", "company", "company name", "name",
                 "legal name", "dba", "organization", "employer_name")
_STATUS_HEADERS = ("status", "account status", "account_status", "case_status")

# Body-shop / OPT-mill name heuristic. These generic IT-staffing shapes are the
# classic fraud-adjacent shops; we SKIP + report them rather than auto-trust them.
# Real IT-services GIANTS (Infosys, Cognizant, HCL, TCS, Wipro, Accenture, Deloitte…)
# do NOT match these patterns, so they're unaffected.
# Single source of truth lives in core.BODYSHOP_RE (also used by the live feed's "Agency"
# badge); fall back to a local copy if core isn't importable in an offline run.
try:
    from core import BODYSHOP_RE as _BODYSHOP_RE
except Exception:
    _BODYSHOP_RE = re.compile(
        r"\b(soft\s*systems?|tech\s*solutions?|software\s*solutions?|it\s*solutions?|"
        r"info(?:tech| systems?| solutions?)|tek\s*solutions?|consultancy services?|"
        r"staffing|technologies\s+inc|solutions\s+inc|systems\s+inc|infotech|"
        r"global\s+(?:it|tech|soft|systems?|solutions?))\b", re.I)

# A few well-known large employers worth flagging even if they aren't currently a
# scrapeable SOURCE (kept tiny + obviously legitimate). Extend as needed.
_ALLOWLIST = (
    "Apple", "Microsoft", "Google", "Meta", "Netflix", "Tesla", "Intel", "Qualcomm",
    "Texas Instruments", "Boeing", "Lockheed Martin", "Raytheon", "Walmart", "Target",
    "Deloitte", "PwC", "EY", "KPMG", "Accenture", "Infosys", "Cognizant", "Capgemini",
    "Wipro", "Tata Consultancy Services", "IBM", "Oracle", "Cisco", "Dell", "HP",
    "Procter & Gamble", "Johnson & Johnson", "Pfizer", "Merck", "AbbVie", "Eli Lilly",
)


def _col_index(header, candidates):
    low = [(h or "").strip().lower() for h in header]
    for c in candidates:
        if c in low:
            return low.index(c)
    return None


def _rows_from_csv(path):
    with open(path, newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        ni = _col_index(header, _NAME_HEADERS)
        si = _col_index(header, _STATUS_HEADERS)
        if ni is None:
            sys.exit("  No employer-name column found. Headers: %s" % header)
        for r in reader:
            if ni < len(r) and r[ni].strip():
                yield r[ni].strip(), (r[si].strip() if si is not None and si < len(r) else "")


def _rows_from_xlsx(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    it = ws.iter_rows(values_only=True)
    header = list(next(it, []))
    ni = _col_index(header, _NAME_HEADERS)
    si = _col_index(header, _STATUS_HEADERS)
    if ni is None:
        sys.exit("  No employer-name column found. Headers: %s" % header)
    for r in it:
        if ni < len(r) and r[ni]:
            yield str(r[ni]).strip(), (str(r[si]).strip() if si is not None and si < len(r) and r[si] else "")


def main():
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        sys.exit(1)

    # 1) Build a normalized set of ENROLLED employer names from the snapshot(s).
    snap_norm = set()
    total = kept = 0
    for p in paths:
        if not os.path.exists(p):
            print("  Skipping (not found):", p)
            continue
        print("  Reading", p, "...")
        ext = os.path.splitext(p)[1].lower()
        gen = _rows_from_xlsx(p) if ext in (".xlsx", ".xlsm") else _rows_from_csv(p)
        for name, status in gen:
            total += 1
            if any(d in status.lower() for d in _DROP_STATUS):
                continue
            kept += 1
            snap_norm.add(scraper._norm_name(name))
    snap_norm.discard("")
    if not snap_norm:
        sys.exit("  No enrolled employer names extracted — check the file/columns.")
    print("  Snapshot: %d rows, %d enrolled, %d distinct normalized names."
          % (total, kept, len(snap_norm)))

    # 2) Our vetted universe: SOURCES companies (real ATS board) + the allowlist.
    vetted = {}
    for _u, _t, company in scraper.SOURCES:
        if company:
            vetted.setdefault(scraper._norm_name(company), company)
    for company in _ALLOWLIST:
        vetted.setdefault(scraper._norm_name(company), company)

    # 3) Match: a vetted company is E-Verify if its normalized name is in the
    #    snapshot exactly, or the snapshot has a name that starts with it
    #    ("amazon" vs "amazon com services"). Skip body-shop-shaped names.
    enrolled, skipped, unmatched = [], [], []
    snap_list = sorted(snap_norm)
    for norm, canonical in sorted(vetted.items(), key=lambda kv: kv[1].lower()):
        if _BODYSHOP_RE.search(canonical):
            skipped.append(canonical)
            continue
        hit = norm in snap_norm or any(s == norm or s.startswith(norm + " ") for s in snap_list)
        (enrolled if hit else unmatched).append(canonical)

    # 4) Merge with any existing everify.txt and write sorted.
    existing = set()
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            existing = {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
    combined = sorted(set(enrolled) | existing, key=str.lower)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("# E-Verify enrolled employer names (one per line). Built by build_everify.py\n")
        f.write("# from an E-Verify snapshot, scoped to vetted employers. '#' lines ignored.\n")
        f.write("# A HINT only — confirm current status at e-verify.gov before relying on it.\n")
        for n in combined:
            f.write(n + "\n")

    print("\n  Wrote %d names to %s (kept %d existing, added %d new)."
          % (len(combined), OUT, len(existing), len(combined) - len(existing)))
    if skipped:
        print("  Skipped %d body-shop-shaped name(s) (review): %s"
              % (len(skipped), ", ".join(skipped[:30])))
    if unmatched:
        print("  %d vetted companies NOT in the snapshot (name-variant miss or not enrolled):"
              % len(unmatched))
        print("    " + ", ".join(unmatched[:40]) + (" ..." if len(unmatched) > 40 else ""))


if __name__ == "__main__":
    main()
