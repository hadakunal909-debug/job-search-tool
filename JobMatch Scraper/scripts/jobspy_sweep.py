#!/usr/bin/env python3
"""
jobspy_sweep.py — the daily aggregator sweep. A SIDECAR, not a feed source.

WHAT IT IS FOR. Once a day, ask LinkedIn/Indeed (and jobright, when it will answer) what is
being posted, and record it in ONE place: the `jobspy_findings` table. The value is the
employers, not the postings — a company appearing here that we do not scrape is a board worth
adopting, and the posting is the evidence for that.

WHAT IT DELIBERATELY DOES NOT DO. It does not write `jobs`. It does not write `boards`. It does
not touch any company table, and it adopts nothing. CLAUDE.md says "don't reach for a job
aggregator" and that rule is not being reversed here: Adzuna was 6% of the feed and 38% of every
job with no usable description, and routing these rows into the corpus would repeat it exactly.
Adoption stays the deliberate, reviewable step it already is (probe_discovered -> adopt).

WRITES NOTHING WITHOUT --apply, the same guarantee scripts/jobspy_shadow.py makes. A bare run
harvests, screens, probes and writes its report file; only --apply reaches the database, and
even then only `jobspy_findings`.

WHY THE SCREEN IS RIGHT IN CI AND WRONG ON A LAPTOP. `company_is_new` joins against SOURCES *and*
the boards table. The boards table needs database credentials, and without them db.list_boards()
returns [] silently -- so every already-adopted board reads as new. Measured 2026-09-03: Google,
IBM, Tesla, Qualcomm, Microsoft and Apple are all scraped daily and NONE of them is in SOURCES,
because they live in that table. On a credentialled run the number means something; without
credentials this script says so in the header rather than quietly overcounting.

    python scripts/jobspy_sweep.py --sites indeed --hours 24 -v
    python scripts/jobspy_sweep.py --sites indeed,linkedin --phrases all --apply
"""
import argparse
import collections
import csv
import datetime
import importlib.util
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db
import scraper
from scraper import find_everify_boards as feb
from scraper.bounded_call import call as bounded_call

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover_companies as dc                                          # noqa: E402

# Entry/associate phrases across the role families, each checked through scraper.title_verdict
# before being added -- the same bar dc.ROLE_PHRASES was built to. A phrase the title filter
# excludes cannot contribute a scoreable row, so querying it only spends the run's budget.
ENTRY_PHRASES = (
    "associate project manager", "junior project manager", "project coordinator",
    "program coordinator", "associate product manager", "junior product manager",
    "product coordinator", "associate program manager", "associate business analyst",
    "junior business analyst", "entry level business analyst", "junior data analyst",
    "entry level data analyst", "associate data analyst", "junior software engineer",
    "entry level software engineer", "associate software engineer", "junior data engineer",
    "operations coordinator", "project analyst", "program analyst", "associate data engineer",
)

# jobright role slugs that its h1b landing template actually serves. Validated 2026-09-03; the
# other 60 tried returned no server-rendered rows. See scraper.scrape_jobright.
JOBRIGHT_ROLES = ("project-manager", "program-manager", "product-manager", "project-coordinator",
                  "program-coordinator", "product-coordinator", "associate-project-manager",
                  "junior-project-manager")


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def known_companies():
    """Everything we already scrape: SOURCES + the boards table. (set, boards_table_readable)."""
    known = {_norm(n) for _u, _k, n in scraper.SOURCES}
    try:
        boards = db.list_boards() or []
    except Exception:
        boards = []
    for b in boards:
        n = _norm(b.get("company"))
        if n:
            known.add(n)
    known.discard("")
    return known, bool(boards)


def harvest(sites, phrases, location, hours, results, verbose, deadline=None, query_timeout=90):
    """Aggregator postings as {company,title,url,location,posted,channel,phrase} records."""
    rows = []
    # Interleave sources so a blocked source cannot consume every query slot.
    for phrase in phrases:
        for site in sites:
            remaining = deadline - time.monotonic() if deadline else query_timeout
            if remaining <= 0:
                print("  Harvest budget reached; keeping %d posting(s)." % len(rows), flush=True)
                return rows
            try:
                rows.extend(bounded_call("scripts.discover_companies", "harvest_jobspy",
                            ([site], [phrase], location, hours, results),
                            {"verbose": verbose}, timeout=min(query_timeout, remaining)))
            except (TimeoutError, RuntimeError) as exc:
                print("  %s / %s: %s; continuing." % (site, phrase, exc), flush=True)
    return rows


def harvest_jobright(roles, city, verbose, deadline=None, query_timeout=90):
    """jobright's public landing pages, best effort.

    Kept separate from the JobSpy channels because its failure mode is different and matters:
    jobright sits behind Cloudflare and answers a flagged client with a 200 carrying a challenge
    page, not an error. scrape_jobright already reports that as "served no __NEXT_DATA__". A
    datacenter IP (which is what CI is) is exactly what gets flagged, so this is expected to
    return nothing from a runner and the sweep must treat that as normal, not as a failure.
    """
    out, blocked = [], 0
    for role in roles:
        remaining = deadline - time.monotonic() if deadline else query_timeout
        if remaining <= 0:
            break
        try:
            got = bounded_call("scraper", "scrape_jobright", ("jobright:%s|%s" % (role, city),),
                               timeout=min(query_timeout, remaining))
        except Exception as e:
            if verbose:
                print("  jobright %s: %s" % (role, str(e)[:70]))
            got = []
        if not got:
            blocked += 1
            continue
        for r in got:
            out.append({"company": r.get("company"), "title": r.get("title"),
                        "url": r.get("url"), "location": r.get("location"),
                        "posted": r.get("found_date"), "channel": "jobright", "phrase": role})
    if blocked:
        print("  note: jobright returned nothing for %d/%d role(s) — Cloudflare challenge is the "
              "usual cause from a datacenter IP" % (blocked, len(roles)))
    return out


def enrich(company, visa_index, sponsor_counts):
    """(visa_routes, strength_tier, filing_count) for a company.

    The COUNT is returned, not just the tier, because it is what a human reviewing the sheet
    actually reads: "sponsors" is a verdict, 2,069 certified filings is the evidence, and the
    two answer different questions about a company nobody here has heard of. sponsor_strength
    already computes both -- discarding the number was pure loss. It lands in `h1b_filings`,
    which was hardcoded None on every row until 2026-09-09.

    `visa_routes` carries stem_opt when the employer is E-Verify enrolled, which is the actual
    STEM-OPT prerequisite -- see core.VISA_TAG_LABELS.
    """
    tags = core.visa_tags(company, visa_index) or []
    try:
        strength, count = core.sponsor_strength(company, sponsor_counts)
    except Exception:
        strength, count = "", 0
    return ", ".join(tags), strength, int(count or 0)


def probe(company):
    """(career_page, board_url, ats) for a company, or blanks. Never raises."""
    try:
        _c, board, ats, _n, _conf = feb.discover(company)
    except Exception:
        return "", "", ""
    if board:
        return "", board, ats
    # discover() returns the reason in `ats` when it found no board; a careers page it reached
    # is still worth recording, because it is what a human needs to check the company by hand.
    return "", "", ""


def main():
    p = argparse.ArgumentParser(description="Daily aggregator sweep -> jobspy_findings.")
    p.add_argument("--sites", default="indeed", help="comma-separated JobSpy sites")
    p.add_argument("--phrases", default="entry",
                   help="'entry' (default), 'all' (dc.ALL_PHRASES), or pipe-separated")
    p.add_argument("--location", default="United States")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--results", type=int, default=50)
    p.add_argument("--jobright", action="store_true", help="also try jobright's landing pages")
    p.add_argument("--probe-limit", type=int, default=60,
                   help="how many NEW companies to probe for a board (0 = none)")
    p.add_argument("--budget-min", type=float, default=20,
                   help="total network budget; completed results are still saved")
    p.add_argument("--query-timeout", type=float, default=90,
                   help="maximum seconds for one external query or board probe")
    p.add_argument("--out", default="", help="report path (.xlsx or .csv)")
    p.add_argument("--apply", action="store_true", help="write to the jobspy_findings table")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    if a.budget_min <= 0 or a.query_timeout <= 0:
        p.error("--budget-min and --query-timeout must be positive")
    deadline = time.monotonic() + a.budget_min * 60

    sites = [s.strip() for s in a.sites.replace(",", " ").split() if s.strip()]
    if sites and importlib.util.find_spec("jobspy") is None:
        p.error("python-jobspy is required; install requirements.txt before running the sweep")
    if a.phrases == "entry":
        phrases = list(ENTRY_PHRASES)
    elif a.phrases == "all":
        phrases = list(dc.ALL_PHRASES)
    else:
        phrases = [x.strip() for x in a.phrases.split("|") if x.strip()]

    run_date = datetime.date.today().isoformat()
    print("jobspy_sweep %s — sites=%s phrases=%d hours=%d" %
          (run_date, ",".join(sites) or "(none)", len(phrases), a.hours))

    rows = harvest(sites, phrases, a.location, a.hours, a.results, a.verbose,
                   deadline, a.query_timeout)
    if a.jobright:
        rows += harvest_jobright(JOBRIGHT_ROLES, "united-states", a.verbose,
                                 deadline, a.query_timeout)
    print("  harvested %d posting(s)" % len(rows))
    if not rows:
        print("  nothing harvested — every channel returned empty. NOT the same as a quiet day; "
              "check for a block before believing it.")

    known, boards_readable = known_companies()
    if not boards_readable:
        print("  WARNING: the boards table is unreadable (no DB credentials), so company_is_new "
              "counts ONLY against SOURCES and will overstate. Numbers from this run are a "
              "ceiling, not a count.")

    visa_index = core.load_visa_tags()
    sponsor_counts = core.load_sponsor_counts()

    # One probe per NEW company, not per posting: a company with 30 postings is one board.
    new_names, seen_url = [], set()
    # The board we did not have to guess. Indeed's job_url_direct is the EMPLOYER'S OWN ATS link
    # and scraper._jobspy_url prefers it, so 93% of indeed rows already carry one -- measured over
    # 4,119 ledger rows on 2026-09-14, detect_board reads a board out of them for 177 employers
    # where the name probe, which guesses careers.<slug>.com, found 15. Pure string parsing, no
    # network, and it applies to every row rather than the first --probe-limit names. linkedin and
    # jobright serve every posting on their own domain, so they contribute nothing here.
    from_url = {}
    findings = []
    for r in rows:
        url, company = (r.get("url") or "").strip(), (r.get("company") or "").strip()
        if not url or not company or url in seen_url:
            continue
        seen_url.add(url)
        is_new = _norm(company) not in known
        if is_new and company not in new_names:
            new_names.append(company)
        try:
            hit = scraper.detect_board(url)
        except Exception:
            hit = None
        if hit:
            from_url.setdefault(_norm(company), (hit[0], hit[1]))
        routes, strength, filings = enrich(company, visa_index, sponsor_counts)
        findings.append({
            "url": url, "title": (r.get("title") or "").strip(), "company": company,
            "posted_date": (r.get("posted") or "")[:10] or None,
            "location": (r.get("location") or "").strip(),
            "source": r.get("channel") or "",
            "career_page": "", "board_url": hit[0] if hit else "", "ats_type": hit[1] if hit else "",
            "seniority": "", "salary": "",
            "h1b_filings": filings or None, "visa_routes": routes,
            "company_is_new": is_new, "run_date": run_date, "created_at": None,
            "_strength": strength,
        })

    print("  %d unique posting(s); %d company/ies not already scraped" % (len(findings), len(new_names)))
    # Bank the harvest before optional company discovery; the report also survives
    # an unrelated database/probe failure later in the run.
    out = a.out or ("jobspy_findings_%s.xlsx" % run_date)
    write_report(findings, out)
    if a.apply:
        _written, error = db.add_findings(findings)
        if error:
            raise RuntimeError("Could not persist harvested findings: %s" % error)

    came_with_board = [n for n in new_names if _norm(n) in from_url]
    print("  %d new company/ies came with a readable board url of their own" % len(came_with_board))

    boards = {}
    unresolved = [n for n in new_names if _norm(n) not in from_url]
    if a.probe_limit and unresolved:
        # Spend the probe budget on the names that actually need guessing.
        todo = unresolved[:a.probe_limit]
        print("  probing %d of %d new companies with no board url for a board…"
              % (len(todo), len(unresolved)))
        for name in todo:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("  Probe budget reached; saving completed findings.", flush=True)
                break
            try:
                cp, burl, ats = bounded_call("scripts.jobspy_sweep", "probe", (name,),
                                            timeout=min(a.query_timeout, remaining))
            except (TimeoutError, RuntimeError) as exc:
                print("  Probe %s: %s; continuing." % (name, exc), flush=True)
                continue
            if burl:
                boards[_norm(name)] = (cp, burl, ats)
                if a.verbose:
                    print("    HIT %-32s %-14s %s" % (name[:32], ats, burl[:60]))
        print("  found %d board(s) behind the new companies" % len(boards))

    for f in findings:
        # A board read off the employer's own posting url outranks a guessed slug, so the probe
        # only fills the rows that have nothing.
        hit = boards.get(_norm(f["company"]))
        if hit and not f["board_url"]:
            f["career_page"], f["board_url"], f["ats_type"] = hit

    write_report(findings, out)

    if a.apply:
        written, err = db.add_findings(findings)
        print("  wrote %d row(s) to %s%s" % (written, db.FINDINGS_TABLE,
                                             (" — ERROR: " + err) if err else ""))
        if err:
            raise RuntimeError("Could not persist enriched findings: %s" % err)
    else:
        print("  --apply not given, so nothing was written to the database.")

    by_src = collections.Counter(f["source"] for f in findings)
    print("\nby source: %s" % dict(by_src))
    print("new companies with a board found: %d (%d from their own url, %d probed)"
          % (len(came_with_board) + len(boards), len(came_with_board), len(boards)))
    return 0


def write_report(findings, path):
    """One row per posting. .xlsx when openpyxl is present and the name asks for it, else .csv."""
    cols = ["run_date", "posted_date", "company", "company_is_new", "title", "location",
            "source", "career_page", "board_url", "ats_type", "visa_routes",
            "h1b_filings", "_strength", "url"]
    rows = sorted(findings, key=lambda f: (not f["company_is_new"], f["company"].lower()))
    if path.lower().endswith(".xlsx"):
        try:
            import openpyxl
        except ImportError:
            path = path[:-5] + ".csv"
        else:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "findings"
            ws.append([c.lstrip("_") for c in cols])
            for f in rows:
                ws.append([f.get(c) for c in cols])
            ws.freeze_panes = "A2"
            for i, c in enumerate(cols, 1):
                ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = \
                    max(12, min(46, len(c) + 8))
            wb.save(path)
            print("  wrote %s (%d rows)" % (path, len(rows)))
            return
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow([c.lstrip("_") for c in cols])
        for f in rows:
            w.writerow([f.get(c) for c in cols])
    print("  wrote %s (%d rows)" % (path, len(rows)))


if __name__ == "__main__":
    raise SystemExit(main())
