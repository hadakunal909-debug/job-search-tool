#!/usr/bin/env python3
"""One spreadsheet answering "can this employer support a STEM OPT extension?" for every company
discover_companies.py found.

WHAT THE ANSWER CAN AND CANNOT BE, because this is the whole point of the report. STEM OPT
requires only that the employer be enrolled in E-Verify (8 CFR 214.2(f)(10)(ii)(C)). We hold two
federal datasets locally, and NEITHER is a complete E-Verify registry:

  visa_tags.json `stem_opt` bit  -- built from the USCIS E-Verify+ export, which is a small
        OPT-IN PILOT, not the ~1M-employer registry. Measured 2026-08-23: 23 of 25 employers
        that are certainly enrolled (federal contractors are legally required to be) carry NO
        tag -- Google, Intel, IBM, Meta, Apple, Nvidia, Lockheed, Northrop, Boeing, Goldman,
        Tesla, Salesforce, Cisco, Deloitte, Accenture, Infosys. So a missing tag is close to
        meaningless.
  sponsor_counts.json            -- H-1B/LCA filing volume. Proves the employer sponsors work
        visas, which is strong evidence they hire international staff, but is NOT proof of
        E-Verify enrolment.

So the report NEVER says "no". It grades into CONFIRMED / LIKELY / UNKNOWN and gives every row a
one-click e-verify.gov search link, because that tool (a Tableau app on a USCIS host, refreshed
daily at 2am ET) is the only authoritative per-employer answer and it has no bulk download.
Treating "UNKNOWN" as "does not sponsor" is the single most expensive mistake available here --
absence of a federal record is not evidence of absence (core.VISA_ABSENCE_NOTE).

    python scripts/report_stem_opt_xlsx.py
    python scripts/report_stem_opt_xlsx.py --csv discovered_2wk.csv --out stem_opt_report.xlsx
    python scripts/report_stem_opt_xlsx.py --new-only        # skip companies we already scrape
"""
import argparse
import csv
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scraper.report_companies_xlsx import _write_sheet, YELLOW, HEADER_BG, ZEBRA  # noqa: F401

OUT = "stem_opt_report.xlsx"

# Grade labels. Ordered best-evidence first; the sort and the sheet split both key on this.
CONFIRMED = "CONFIRMED - E-Verify enrolled"
LIKELY_H1B = "LIKELY - sponsors H-1B"
LIKELY_EXEMPT = "LIKELY - cap-exempt employer"
UNKNOWN = "UNKNOWN - no federal record"
_GRADE_ORDER = {CONFIRMED: 0, LIKELY_H1B: 1, LIKELY_EXEMPT: 2, UNKNOWN: 3}

HEADERS = ["company", "stem_opt", "evidence", "h1b_filings", "visa_tags", "cap_exempt",
           "postings_seen", "on_target_titles", "sample_title", "already_scraped",
           "staffing_agency", "channel", "verify at e-verify.gov", "jobs on LinkedIn"]
WIDTHS = [34, 26, 40, 11, 22, 11, 12, 15, 42, 14, 14, 16, 46, 46]


def _everify_url(name):
    """The authoritative per-employer check. No bulk download exists, so a link is the honest
    thing to ship -- the user resolves any row they care about in one click."""
    return "https://www.e-verify.gov/e-verify-employer-search?search=%s" % urllib.parse.quote(name)


def _linkedin_url(name):
    return ("https://www.linkedin.com/jobs/search/?keywords=%s&location=United%%20States"
            % urllib.parse.quote(name))


def grade(row):
    """(label, evidence) for one screened company. Never returns a negative."""
    tags = (row.get("visa_tags") or "").split("|")
    tags = [t for t in tags if t]
    try:
        h1b = int(row.get("h1b_filings") or 0)
    except ValueError:
        h1b = 0

    if (row.get("stem_opt") or "").lower() == "yes":
        return CONFIRMED, "Listed in the USCIS E-Verify+ employer export"
    if h1b > 0:
        others = ", ".join(t for t in tags if t != "stem_opt")
        return LIKELY_H1B, ("%d H-1B/LCA filings%s - sponsors work visas, but E-Verify "
                            "enrolment is unconfirmed; verify at e-verify.gov"
                            % (h1b, " (%s)" % others if others else ""))
    if (row.get("cap_exempt") or "").lower() == "yes":
        return LIKELY_EXEMPT, ("Cap-exempt (university/hospital) - the best H-1B route, no "
                               "lottery; E-Verify enrolment unconfirmed")
    return UNKNOWN, ("No federal filing record and not in the E-Verify+ export. This is NOT "
                     "evidence against sponsorship - 10% of the corpus and 33% of jobs above "
                     "the match floor have no record either. Verify at e-verify.gov")


def build(rows, new_only=False):
    out = []
    for r in rows:
        if new_only and (r.get("in_sources") or "").lower() == "yes":
            continue
        label, evidence = grade(r)
        name = (r.get("company") or "").strip()
        out.append({
            "company": name,
            "stem_opt": label,
            "evidence": evidence,
            "h1b_filings": int(r.get("h1b_filings") or 0),
            "visa_tags": r.get("visa_tags") or "",
            "cap_exempt": r.get("cap_exempt") or "no",
            "postings_seen": int(r.get("postings_seen") or 0),
            "on_target_titles": int(r.get("pm_titles_kept") or 0),
            "sample_title": r.get("sample_title") or "",
            "already_scraped": r.get("in_sources") or "no",
            "staffing_agency": r.get("bodyshop") or "no",
            "channel": r.get("channel") or "",
            "verify at e-verify.gov": _everify_url(name),
            "jobs on LinkedIn": _linkedin_url(name),
        })
    out.sort(key=lambda r: (_GRADE_ORDER.get(r["stem_opt"], 9), -r["h1b_filings"],
                            -r["on_target_titles"], r["company"].lower()))
    return out


def _summary_rows(rows, src):
    n = len(rows)
    def pct(k):
        return "%d (%.0f%%)" % (k, (100.0 * k / n) if n else 0)
    conf = sum(1 for r in rows if r["stem_opt"] == CONFIRMED)
    h1b = sum(1 for r in rows if r["stem_opt"] == LIKELY_H1B)
    exempt = sum(1 for r in rows if r["stem_opt"] == LIKELY_EXEMPT)
    unk = sum(1 for r in rows if r["stem_opt"] == UNKNOWN)
    new = sum(1 for r in rows if r["already_scraped"] != "yes")
    return [
        {"item": "Source file", "value": src},
        {"item": "Companies in report", "value": str(n)},
        {"item": "Not yet scraped by JobMatch", "value": pct(new)},
        {"item": "", "value": ""},
        {"item": CONFIRMED, "value": pct(conf)},
        {"item": LIKELY_H1B, "value": pct(h1b)},
        {"item": LIKELY_EXEMPT, "value": pct(exempt)},
        {"item": UNKNOWN, "value": pct(unk)},
        {"item": "", "value": ""},
        {"item": "How to read this",
         "value": "STEM OPT requires only that the employer be enrolled in E-Verify."},
        {"item": "",
         "value": "CONFIRMED means they appear in the USCIS E-Verify+ export."},
        {"item": "",
         "value": "UNKNOWN does NOT mean no. Our E-Verify data is a small opt-in pilot, "
                  "not the ~1M-employer registry."},
        {"item": "",
         "value": "Measured: 23 of 25 employers that are certainly enrolled carry no tag "
                  "(Google, Intel, Lockheed, Deloitte, Infosys...)."},
        {"item": "",
         "value": "So use the e-verify.gov link on any row you care about - it is the only "
                  "authoritative per-employer answer and refreshes daily."},
        {"item": "",
         "value": "staffing_agency=yes means the name matches the OPT/H-1B body-shop pattern. "
                  "Treat an E-Verify badge there with suspicion."},
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="discovered_2wk.csv",
                    help="discover_companies.py output")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--new-only", action="store_true",
                    help="exclude companies already in SOURCES / the boards table")
    a = ap.parse_args()

    if not os.path.exists(a.csv):
        raise SystemExit("no such file: %s (run scripts/discover_companies.py first)" % a.csv)
    with open(a.csv, encoding="utf-8-sig", newline="") as f:
        src_rows = list(csv.DictReader(f))
    rows = build(src_rows, new_only=a.new_only)
    if not rows:
        raise SystemExit("nothing to report")

    from openpyxl import Workbook
    wb = Workbook()

    ws = wb.active
    ws.title = "Summary"
    _write_sheet(ws, ["item", "value"], _summary_rows(rows, a.csv), [34, 108])

    _write_sheet(wb.create_sheet("All Companies"), HEADERS, rows, WIDTHS,
                 highlight=lambda r: r["stem_opt"] == CONFIRMED)

    for title, want in (("STEM OPT Confirmed", (CONFIRMED,)),
                        ("Likely", (LIKELY_H1B, LIKELY_EXEMPT)),
                        ("Unknown - verify by hand", (UNKNOWN,))):
        subset = [r for r in rows if r["stem_opt"] in want]
        if subset:
            _write_sheet(wb.create_sheet(title), HEADERS, subset, WIDTHS,
                         highlight=lambda r: r["stem_opt"] == CONFIRMED)

    wb.save(a.out)
    print("companies       : %d" % len(rows))
    for label in (CONFIRMED, LIKELY_H1B, LIKELY_EXEMPT, UNKNOWN):
        print("  %-32s %d" % (label, sum(1 for r in rows if r["stem_opt"] == label)))
    print("wrote %s" % os.path.abspath(a.out))


if __name__ == "__main__":
    main()
