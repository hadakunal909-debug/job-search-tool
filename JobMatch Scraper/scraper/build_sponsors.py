#!/usr/bin/env python3
"""
build_sponsors.py — turn public U.S. DOL H1B disclosure data into sponsors.txt
------------------------------------------------------------------------------
The scraper can FLAG which employers have sponsored H1B before. To power that it
reads sponsors.txt (one employer name per line). This script builds/expands that
file from the Department of Labor's free LCA (H-1B) disclosure data.

How to use:
  1. Download the latest "LCA Programs (H-1B, H-1B1, E-3)" disclosure file (.xlsx)
     from the DOL OFLC Performance Data page:
         https://www.dol.gov/agencies/eta/foreign-labor/performance
     (One big Excel file per fiscal year / quarter.)
  2. Run:
         pip install openpyxl
         python build_sponsors.py "LCA_Disclosure_Data_FY2026_Q2.xlsx"
  3. It reads the EMPLOYER_NAME column (keeping only CERTIFIED cases by default),
     de-duplicates, MERGES with whatever is already in sponsors.txt (so the seed
     list shipped with this tool is preserved), and writes sponsors.txt sorted.

Combine several files (more years = more employers flagged):
         python build_sponsors.py FY2025.xlsx FY2026_Q1.xlsx FY2026_Q2.xlsx

The DOL file is large; this uses openpyxl's read-only streaming so it won't eat
your RAM. A .csv export works too (same column names).
"""
import os
import sys
import csv

OUT = "sponsors.txt"
CERTIFIED_ONLY = True   # set False to also include withdrawn/denied employers


def _name_status_cols(header):
    up = [(h or "").strip().upper() for h in header]
    name_i = up.index("EMPLOYER_NAME") if "EMPLOYER_NAME" in up else None
    stat_i = up.index("CASE_STATUS") if "CASE_STATUS" in up else None
    return name_i, stat_i


def from_xlsx(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    name_i, stat_i = _name_status_cols(next(rows))
    if name_i is None:
        sys.exit("  No EMPLOYER_NAME column found in %s" % path)
    out = set()
    for r in rows:
        if name_i >= len(r) or not r[name_i]:
            continue
        if CERTIFIED_ONLY and stat_i is not None and stat_i < len(r):
            if "CERTIFIED" not in str(r[stat_i] or "").upper():
                continue
        out.add(str(r[name_i]).strip())
    return out


def from_csv(path):
    out = set()
    with open(path, newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.reader(f)
        name_i, stat_i = _name_status_cols(next(reader))
        if name_i is None:
            sys.exit("  No EMPLOYER_NAME column found in %s" % path)
        for r in reader:
            if name_i >= len(r) or not r[name_i].strip():
                continue
            if CERTIFIED_ONLY and stat_i is not None and stat_i < len(r):
                if "CERTIFIED" not in r[stat_i].upper():
                    continue
            out.add(r[name_i].strip())
    return out


def main():
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        sys.exit(1)
    names = set()
    for p in paths:
        if not os.path.exists(p):
            print("  Skipping (not found):", p)
            continue
        print("  Reading", p, "...")
        ext = os.path.splitext(p)[1].lower()
        names |= from_xlsx(p) if ext in (".xlsx", ".xlsm") else from_csv(p)
    if not names:
        sys.exit("  No employer names extracted — check the file and its columns.")
    existing = set()
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            existing = {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
    combined = sorted(existing | names, key=str.lower)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("# H1B sponsor employer names (one per line). Built by build_sponsors.py\n")
        f.write("# from DOL LCA disclosure data + your seed list. '#' lines are ignored.\n")
        for n in combined:
            f.write(n + "\n")
    print("  Wrote %d names to %s (kept %d existing, added %d new)."
          % (len(combined), OUT, len(existing), len(combined) - len(existing)))


if __name__ == "__main__":
    main()
