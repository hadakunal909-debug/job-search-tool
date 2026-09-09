#!/usr/bin/env python3
"""
review_findings.py -- read the aggregator sweep ledger and rank the employers worth a board.

WHAT THIS IS FOR. jobspy_sweep.py writes one row per POSTING per day. The question a human
actually has is per EMPLOYER and across days: who has turned up that we do not scrape, and does
the sponsorship record suggest they would hire me? That is a fold, not a filter, and it is the
reason the findings live in a table rather than only in each run's spreadsheet -- one run's sheet
cannot tell you that a company has appeared on four consecutive days.

RANKING. Staffing firms LAST, then certified H-1B filings (a count, descending), then STEM-OPT
evidence, then how many postings the employer has run. Deliberately NOT a single score: the
inputs are a count and two booleans, so combining them would hide which one fired, and this
sheet exists to be read rather than sorted on. See core.VISA_TAG_LABELS -- `stem_opt` means the
employer is E-Verify enrolled, which is the real STEM-OPT prerequisite, and absence is NOT
evidence of a non-sponsor.

WHY STAFFING FIRMS SORT LAST RATHER THAN BEING DROPPED. The top of any H-1B volume ranking is
body shops -- the first run of this ranked Infosys, TCS, Mphasis, Kforce, Iris Software and
EPITEC into the top 21, which is a true fact about filing volume and a useless answer to "whose
board should I adopt". scripts/rank_new_sponsors.py drops them outright for that reason, and it
is right to: it is feeding a probe budget. This is feeding a human, so they are ordered down and
labelled instead -- a review sheet that silently omits rows teaches you to distrust it. The test
is core.is_agency / core.BODYSHOP_RE, reused rather than re-guessed.

    python scripts/review_findings.py                  # new employers, every run held
    python scripts/review_findings.py --days 7
    python scripts/review_findings.py --all            # include ones we already scrape
    python scripts/review_findings.py --out review.xlsx
"""
import argparse
import collections
import csv
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db

COLS = ["company", "agency", "postings", "runs", "first_run", "last_run", "h1b_filings",
        "visa_routes", "board_url", "ats_type", "sources", "example_url"]


def fold(rows):
    """One record per employer, folded from many posting rows."""
    by = collections.OrderedDict()
    for r in rows:
        name = (r.get("company") or "").strip()
        if not name:
            continue
        g = by.setdefault(name, {"company": name, "postings": 0, "_runs": set(),
                                 "h1b_filings": 0, "_routes": set(), "board_url": "",
                                 "ats_type": "", "_sources": set(), "example_url": "",
                                 "company_is_new": False,
                                 # core.is_agency covers the named-agency list, BODYSHOP_RE the
                                 # shape ("... Solutions Inc", "... Staffing"). Both, because
                                 # neither alone catches Kforce and Iris Software together.
                                 "agency": bool(core.is_agency(name)
                                                or core.BODYSHOP_RE.search(name or ""))})
        g["postings"] += 1
        if r.get("run_date"):
            g["_runs"].add(str(r["run_date"])[:10])
        # New if ANY run said so: the flag is computed per run, and an adoption between runs
        # flips it -- which must not retroactively hide the finding that led to the adoption.
        g["company_is_new"] = g["company_is_new"] or bool(r.get("company_is_new"))
        try:
            g["h1b_filings"] = max(g["h1b_filings"], int(r.get("h1b_filings") or 0))
        except (TypeError, ValueError):
            pass
        for t in (r.get("visa_routes") or "").split(","):
            if t.strip():
                g["_routes"].add(t.strip())
        if r.get("source"):
            g["_sources"].add(r["source"])
        if r.get("board_url") and not g["board_url"]:
            g["board_url"], g["ats_type"] = r["board_url"], (r.get("ats_type") or "")
        if not g["example_url"]:
            g["example_url"] = r.get("url") or ""
    out = []
    for g in by.values():
        g["runs"] = len(g["_runs"])
        g["first_run"] = min(g["_runs"]) if g["_runs"] else ""
        g["last_run"] = max(g["_runs"]) if g["_runs"] else ""
        g["visa_routes"] = ", ".join(sorted(g.pop("_routes")))
        g["sources"] = ", ".join(sorted(g.pop("_sources")))
        g.pop("_runs")
        out.append(g)
    return out


def rank(recs):
    return sorted(recs, key=lambda g: (bool(g.get("agency")),
                                       -int(g.get("h1b_filings") or 0),
                                       "stem_opt" not in (g.get("visa_routes") or ""),
                                       -int(g.get("postings") or 0),
                                       (g.get("company") or "").lower()))


def main():
    p = argparse.ArgumentParser(description="Rank employers from the sweep ledger.")
    p.add_argument("--days", type=int, default=0, help="only runs within N days (0 = all)")
    p.add_argument("--all", action="store_true", help="include employers we already scrape")
    p.add_argument("--limit", type=int, default=40, help="rows to print (0 = all)")
    p.add_argument("--out", default="", help="also write .xlsx/.csv")
    a = p.parse_args()

    if not db.has_remote_db():
        print("No remote database configured, so the ledger cannot be read. Set DB_PROXY_URL and")
        print("DB_PROXY_SECRET (docs/OPERATIONS.md) -- a local run would report a confident zero.")
        return 2

    rows = db.list_findings()
    print("ledger: %d posting row(s) from %s" % (len(rows), db.backend_name()))
    if not rows:
        print("Nothing recorded yet. The sweep writes on its 16:00 UTC schedule, or run it now:")
        print("  python scripts/jobspy_sweep.py --sites indeed,linkedin --jobright --apply -v")
        return 0

    if a.days:
        floor = (datetime.date.today() - datetime.timedelta(days=a.days)).isoformat()
        rows = [r for r in rows if str(r.get("run_date") or "")[:10] >= floor]
        print("        %d row(s) within %d day(s)" % (len(rows), a.days))

    recs = fold(rows)
    total = len(recs)
    if not a.all:
        recs = [g for g in recs if g.get("company_is_new")]
    recs = rank(recs)
    print("        %d employer(s)%s" % (
        len(recs), "" if a.all else " of %d not already scraped" % total))
    print("")

    shown = recs if not a.limit else recs[:a.limit]
    print("%-32s %-3s %5s %4s %8s  %-24s %s" % (
        "EMPLOYER", "AGY", "POSTS", "RUNS", "H1B", "ROUTES", "BOARD"))
    for g in shown:
        print("%-32s %-3s %5d %4d %8s  %-24s %s" % (
            (g["company"] or "")[:32], "yes" if g.get("agency") else "", g["postings"],
            g["runs"], g["h1b_filings"] or "-", (g["visa_routes"] or "-")[:24],
            (g["board_url"] or "-")[:40]))
    if a.limit and len(recs) > a.limit:
        print("... and %d more (--limit 0 for all)" % (len(recs) - a.limit))

    withfilings = sum(1 for g in recs if int(g.get("h1b_filings") or 0) > 0)
    withstem = sum(1 for g in recs if "stem_opt" in (g.get("visa_routes") or ""))
    withboard = sum(1 for g in recs if g.get("board_url"))
    print("")
    agencies = sum(1 for g in recs if g.get("agency"))
    print("%d with certified H-1B filings, %d with STEM-OPT (E-Verify) evidence, %d with a board"
          " already found." % (withfilings, withstem, withboard))
    print("%d look like staffing firms and are sorted to the bottom, not removed." % agencies)
    print("No filing record is NOT evidence of a non-sponsor -- most often it means a company too")
    print("small or too new to appear in the disclosure data at all.")

    if a.out:
        write(recs, a.out)
    return 0


def write(recs, path):
    if path.lower().endswith(".xlsx"):
        try:
            import openpyxl
        except ImportError:
            path = path[:-5] + ".csv"
        else:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "employers"
            ws.append(COLS)
            for g in recs:
                ws.append([g.get(c) for c in COLS])
            ws.freeze_panes = "A2"
            for i, c in enumerate(COLS, 1):
                ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = max(
                    12, min(52, len(c) + 10))
            wb.save(path)
            print("wrote %s (%d employers)" % (path, len(recs)))
            return
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(COLS)
        for g in recs:
            w.writerow([g.get(c) for c in COLS])
    print("wrote %s (%d employers)" % (path, len(recs)))


if __name__ == "__main__":
    raise SystemExit(main())
