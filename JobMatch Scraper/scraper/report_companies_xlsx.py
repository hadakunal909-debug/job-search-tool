#!/usr/bin/env python3
"""
report_companies_xlsx.py — one spreadsheet of every company the scraper pulls from, with the
ones added from the E-Verify+ list highlighted in yellow.

Three sheets:
  All Companies   every source — the built-in SOURCES list plus the `boards` DB table that
                  custom_sources() merges in. New rows are filled yellow.
  New Additions   just the newly adopted boards, with the verification evidence that let
                  them in (what the board calls itself, the match score, the verdict).
  Not Added       probed E-Verify+ sponsors that did NOT yield a usable board, and why —
                  the review-block mismatches and the ones with no board at all.

    python -m scraper.report_companies_xlsx
    python -m scraper.report_companies_xlsx --out companies.xlsx
"""
import os
import sys
import csv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scraper
from scraper.adopt_everify_boards import ADDED_BY, OUT as ADOPTION_CSV
from scraper.probe_everify_candidates import REPORT as PROBE_CSV

OUT = "jobmatch_companies.xlsx"
YELLOW = "FFFDE68A"          # amber-200; readable behind black text in both Excel themes
HEADER_BG = "FF1F2937"
ZEBRA = "FFF6F7F9"


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _autosize(ws, widths):
    from openpyxl.utils import get_column_letter
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _write_sheet(ws, headers, rows, widths, highlight=None):
    """highlight(row_dict) -> True to fill the row yellow."""
    from openpyxl.styles import Font, PatternFill, Alignment
    hf = PatternFill("solid", fgColor=HEADER_BG)
    yf = PatternFill("solid", fgColor=YELLOW)
    zf = PatternFill("solid", fgColor=ZEBRA)
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFFFF")
        c.fill = hf
        c.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"
    for n, row in enumerate(rows, start=2):
        ws.append([row.get(h, "") for h in headers])
        fill = yf if (highlight and highlight(row)) else (zf if n % 2 == 0 else None)
        if fill:
            for c in ws[n]:
                c.fill = fill
    ws.auto_filter.ref = ws.dimensions
    _autosize(ws, widths)


def _load_evidence():
    """{board_url: probe/adoption row} merged across every batch on disk.

    Each probe run writes its own CSV and each adopt run OVERWRITES everify_adoption.csv, so no
    single file describes the whole session. The DB is the authority for WHAT was added (see
    collect); these files only supply the detail columns — filings, workforce, and the identity
    evidence — and a missing file just means those cells come out blank.
    """
    import glob
    ev = {}
    for path in sorted(glob.glob("everify_board_probe*.csv")) + \
            sorted(glob.glob("everify_adoption*.csv")):
        for r in _read_csv(path):
            if r.get("board_url"):
                ev.setdefault(r["board_url"], {}).update(
                    {k: v for k, v in r.items() if v not in (None, "")})
    return ev


def _all_probe_rows():
    """Every probed company across all batches. Each probe run writes its own CSV
    (everify_board_probe.csv, everify_board_probe_big.csv, ...) so globbing is what keeps the
    'Not Added' sheet from silently describing only the first run."""
    import glob
    rows, seen = [], set()
    for path in sorted(glob.glob("everify_board_probe*.csv")):
        for r in _read_csv(path):
            key = (r.get("employer", ""), r.get("board_url", ""))
            if key not in seen:
                seen.add(key)
                rows.append(r)
    return rows


def collect():
    """(all_rows, new_rows, not_added_rows) ready to write."""
    adoption = _read_csv(ADOPTION_CSV)
    probe = _all_probe_rows()

    # Everything the scraper will actually pull from this run.
    builtin = [(u, a, c) for u, a, c in scraper.SOURCES]
    try:
        custom = list(scraper.custom_sources())
    except Exception:
        custom = []

    # Which URLs are new from this E-Verify+ pass. Taken from the `boards` table, NOT from the
    # adoption CSV: each adopt run rewrites that file, so after a second batch it describes only
    # the last one and the report would silently under-count what is live.
    evidence = _load_evidence()
    added = {}
    try:
        import db
        for b in db.list_boards():
            if (b.get("added_by") or "") == ADDED_BY and b.get("url"):
                added[b["url"]] = evidence.get(b["url"], {})
    except Exception as e:
        print("warning: could not read the boards table (%s) — falling back to %s"
              % (type(e).__name__, ADOPTION_CSV))
        added = {r["board_url"]: r for r in adoption if r.get("added") == "yes"}

    by_url = {}
    for url, ats, company in builtin:
        by_url.setdefault(url, {"Company": company, "ATS": ats, "Board URL": url,
                                "Origin": "built-in SOURCES", "New": "",
                                "H-1B filings": "", "Postings": "", "Workforce": "",
                                "States": ""})
    for url, ats, company in custom:
        a = added.get(url)
        by_url[url] = {
            "Company": company, "ATS": ats, "Board URL": url,
            "Origin": ("E-Verify+ 2026-08" if a else "boards table (auto-discovered)"),
            "New": "YES" if a else "",
            "H-1B filings": (a or {}).get("h1b_filings", ""),
            "Postings": (a or {}).get("job_count", ""),
            "Workforce": (a or {}).get("size", ""),
            "States": (a or {}).get("state", ""),
        }
    all_rows = sorted(by_url.values(),
                      key=lambda r: (r["New"] != "YES", (r["Company"] or "").lower()))

    new_rows = []
    by_url_custom = {u: (a, c) for u, a, c in custom}
    for url, r in added.items():
        ats, company = by_url_custom.get(url, (r.get("ats_type", ""), r.get("employer", "")))
        new_rows.append({
            "Company": company or r.get("employer", ""), "ATS": ats, "Board URL": url,
            "Postings": r.get("job_count", ""), "H-1B filings": r.get("h1b_filings", ""),
            "Workforce": r.get("size", ""), "States": r.get("state", ""),
            "Bucket": r.get("bucket", ""), "Body-shop flag": r.get("bodyshop", ""),
            "Board reports itself as": r.get("reported_name", ""),
            "Match score": r.get("score", ""), "Verdict": r.get("verdict", ""),
        })
    new_rows.sort(key=lambda r: -(int(r["Postings"]) if str(r["Postings"]).isdigit() else 0))

    not_added = []
    for r in adoption:
        if r.get("added") == "yes":
            continue
        not_added.append({
            "Company": r["employer"], "Why not added":
                ("board belongs to someone else" if r["verdict"] == "review"
                 else "identity unproven (%s)" % r["verdict"]),
            "Board found": r["board_url"], "ATS": r["ats_type"],
            "Board reports itself as": r["reported_name"], "Match score": r["score"],
            "H-1B filings": r["h1b_filings"], "Workforce": r["size"],
        })
    seen_hit = {r["employer"] for r in adoption}
    for r in probe:
        if r["employer"] in seen_hit or r.get("board_url"):
            continue
        not_added.append({
            "Company": r["employer"],
            "Why not added": ("careers page found, no readable ATS" if r.get("career_page")
                              else "no board and no careers page found"),
            "Board found": r.get("career_page", ""), "ATS": "",
            "Board reports itself as": "", "Match score": "",
            "H-1B filings": r["h1b_filings"], "Workforce": r["size"],
        })
    not_added.sort(key=lambda r: -(int(r["H-1B filings"])
                                   if str(r["H-1B filings"]).isdigit() else 0))
    return all_rows, new_rows, not_added


def main():
    out = OUT
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    from openpyxl import Workbook

    all_rows, new_rows, not_added = collect()
    wb = Workbook()

    ws = wb.active
    ws.title = "All Companies"
    _write_sheet(ws,
                 ["Company", "ATS", "Board URL", "Origin", "New",
                  "Postings", "H-1B filings", "Workforce", "States"],
                 all_rows, [38, 16, 62, 30, 6, 10, 13, 17, 14],
                 highlight=lambda r: r.get("New") == "YES")

    ws2 = wb.create_sheet("New Additions")
    _write_sheet(ws2,
                 ["Company", "ATS", "Board URL", "Postings", "H-1B filings", "Workforce",
                  "States", "Bucket", "Body-shop flag", "Board reports itself as",
                  "Match score", "Verdict"],
                 new_rows, [34, 16, 58, 10, 13, 17, 12, 18, 14, 34, 12, 11],
                 highlight=lambda r: True)

    ws3 = wb.create_sheet("Not Added")
    _write_sheet(ws3,
                 ["Company", "Why not added", "Board found", "ATS",
                  "Board reports itself as", "Match score", "H-1B filings", "Workforce"],
                 not_added, [34, 34, 52, 15, 34, 12, 13, 17])

    wb.save(out)
    print("All Companies : %d  (%d highlighted as new)"
          % (len(all_rows), sum(1 for r in all_rows if r["New"] == "YES")))
    print("New Additions : %d" % len(new_rows))
    print("Not Added     : %d" % len(not_added))
    print("wrote %s" % os.path.abspath(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
