#!/usr/bin/env python3
"""
probe_everify_xlsx.py — mine the federal E-Verify(+) employer list for REAL companies
that have a scrapeable job board, and write a review report so the user can cherry-pick.

The raw list (Downloads/Employer List.xlsx, ~26k rows) is mostly tiny businesses and, at
the large end, public-sector/local orgs (school districts, cities, credit unions, local
hospitals) — none of which are the target (H1B-sponsoring, PM/ops/analyst-hiring, scrapeable
ATS). So we FILTER hard first, then run the EXISTING detect chain (reused wholesale from
scraper.find_everify_boards.discover) over the survivors, and emit everify_probe_report.csv.

Filters: Account Status == Open; Workforce Size >= --min-size (default 500); drop body-shops
(build_everify._BODYSHOP_RE); drop public-sector/local (NONTARGET below, but KEEP universities/
colleges — cap-exempt H1B sponsors); dedup vs companies already in scraper.SOURCES.

REVIEW-FIRST: this never touches SOURCES. It only produces the CSV; the user picks from it.

    python -m scraper.probe_everify_xlsx                       # full run, >=500 staff
    python -m scraper.probe_everify_xlsx --limit 30 -v         # smoke test
    python -m scraper.probe_everify_xlsx --min-size 1000 --workers 24
    python -m scraper.probe_everify_xlsx --xlsx "C:/path/to/Employer List.xlsx"
"""
import os
import re
import sys
import csv
import zipfile
import xml.etree.ElementTree as ET
import concurrent.futures

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scraper
from scraper import find_everify_boards as feb   # reuse discover/_fast_session/_reachable/_careers_candidates
from scraper.build_everify import _BODYSHOP_RE

DEFAULT_XLSX = r"C:\Users\k.signhhada\Downloads\Employer List.xlsx"
REPORT = "everify_probe_report.csv"

# Public-sector / local-gov / local-health / religious — NOT the target. Universities and
# colleges are deliberately NOT matched (they're cap-exempt H1B sponsors worth keeping).
NONTARGET = re.compile(
    r"\b(school district|public schools?|independent school|unified school|isd|"
    r"board of education|charter school|education service center|\bschools\b|elementary|"
    r"city of|county of|\bcounty\b|town of|village of|borough of|municipalit|"
    r"fire district|fire department|police|sheriff|water district|water authority|"
    r"transit authority|housing authority|public util|"
    r"credit union|federal credit|hospice|\brehab\b|nursing|skilled nursing|"
    r"assisted living|senior living|health (and|&) rehab|"
    r"church|ministries|ministry|diocese|parish|archdiocese|synagogue|"
    r"department of|state of|commonwealth of)\b", re.I)


# ---------------- stdlib xlsx reader (openpyxl isn't installed) ----------------
def _ln(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def _colnum(ref):
    s = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in s:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx(path):
    """Return (header_list, [row_list, ...]) from the first worksheet. xlsx is a zip of XML:
    we read the shared-strings table + sheet1 cells with the stdlib (no openpyxl needed)."""
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for _, el in ET.iterparse(z.open("xl/sharedStrings.xml"), events=("end",)):
            if _ln(el.tag) == "si":
                shared.append("".join((t.text or "") for t in el.iter() if _ln(t.tag) == "t"))
                el.clear()
    sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    header, rows = None, []
    for _, el in ET.iterparse(z.open(sheets[0]), events=("end",)):
        if _ln(el.tag) != "row":
            continue
        cells = {}
        for c in el:
            if _ln(c.tag) != "c":
                continue
            v, t = None, c.get("t")
            for ch in c:
                if _ln(ch.tag) == "v":
                    v = ch.text
                elif _ln(ch.tag) == "is":
                    v = "".join((x.text or "") for x in ch.iter() if _ln(x.tag) == "t")
            cells[_colnum(c.get("r"))] = "" if v is None else (shared[int(v)] if t == "s" else v)
        row = [cells.get(i, "") for i in range(max(cells) + 1)] if cells else []
        el.clear()
        if header is None:
            header = row
        else:
            rows.append(row)
    return header, rows


def _size_lower(s):
    """Lower bound of a 'Workforce Size' bucket ('500 to 999' -> 500, '10,000 and over' ->
    10000, '5 to 9' -> 5). -1 if unparseable."""
    m = re.match(r"([\d,]+)", (s or "").strip())
    return int(m.group(1).replace(",", "")) if m else -1


def load_candidates(path, min_size):
    """Filtered, deduped list of {employer, dba, size, state, sites} ready to probe."""
    header, rows = read_xlsx(path)
    idx = {h: i for i, h in enumerate(header)}
    emp, dba = idx["Employer"], idx.get("Doing Business As", -1)
    status, size = idx["Account Status"], idx["Workforce Size"]
    state, sites = idx.get("Hiring Site Locations", -1), idx.get("Number of Hiring Sites", -1)

    def g(r, i):
        return r[i] if 0 <= i < len(r) else ""

    have = {scraper._norm_name(c) for _, _, c in scraper.SOURCES}
    seen = set()
    kept, drop = [], {"status": 0, "size": 0, "bodyshop": 0, "public": 0, "dup": 0, "already": 0}
    for r in rows:
        name = g(r, emp).strip()
        if not name:
            continue
        if g(r, status).strip().lower() != "open":
            drop["status"] += 1
            continue
        if _size_lower(g(r, size)) < min_size:
            drop["size"] += 1
            continue
        n = scraper._norm_name(name)
        if not n or n in seen:
            drop["dup"] += 1
            continue
        seen.add(n)
        if _BODYSHOP_RE.search(name):
            drop["bodyshop"] += 1
            continue
        if NONTARGET.search(name):
            drop["public"] += 1
            continue
        if n in have:
            drop["already"] += 1
            continue
        kept.append({"employer": name, "dba": g(r, dba), "size": g(r, size),
                     "state": g(r, state), "sites": g(r, sites)})
    return kept, drop


def probe(rec):
    """Run the existing detect chain for one company; record whether a careers page is even
    reachable when no board is found. Returns the rec augmented with board fields."""
    name = rec["employer"]
    _co, url, ats, cnt, conf = feb.discover(name)
    if url:
        rec.update(career_page="yes", ats_type=ats, job_count=cnt, board_url=url, confidence=conf)
    else:
        cp = ""
        for cand in feb._careers_candidates(name):
            if feb._reachable(cand):
                cp = cand
                break
        rec.update(career_page=cp, ats_type="", job_count="", board_url="", confidence="")
    return rec


def _arg(flag, default=None, cast=str):
    if flag in sys.argv:
        try:
            return cast(sys.argv[sys.argv.index(flag) + 1])
        except (ValueError, IndexError):
            print("%s needs a value" % flag)
            sys.exit(1)
    return default


def main():
    xlsx = _arg("--xlsx", DEFAULT_XLSX)
    min_size = _arg("--min-size", 500, int)
    workers = _arg("--workers", 24, int)
    limit = _arg("--limit", None, int)
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    if not os.path.exists(xlsx):
        print("xlsx not found:", xlsx)
        return

    cands, drop = load_candidates(xlsx, min_size)
    if limit:
        cands = cands[:limit]
    print("Filtered to %d companies to probe (>=%d staff). Dropped: %s"
          % (len(cands), min_size, ", ".join("%s %d" % (k, v) for k, v in drop.items())), flush=True)
    if not cands:
        print("Nothing to probe.")
        return

    # Fast-fail session for the whole run (no retries / short timeouts) — restored after.
    orig = scraper.SESSION
    scraper.SESSION = feb._fast_session()
    results = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(probe, c): c for c in cands}
            done = 0
            for fut in concurrent.futures.as_completed(futs):
                try:
                    rec = fut.result(timeout=50)
                except Exception:
                    rec = dict(futs[fut], career_page="", ats_type="", job_count="",
                               board_url="", confidence="")
                results.append(rec)
                done += 1
                if rec.get("board_url"):
                    print("HIT  %-34s %-15s %-6s %s" % (rec["employer"][:34], rec["ats_type"],
                          rec["job_count"], rec["board_url"][:54]), flush=True)
                elif verbose:
                    print("  -- %-34s %s" % (rec["employer"][:34],
                          rec["career_page"] or "no board / no careers page"), flush=True)
                if done % 50 == 0:
                    print("  ... %d/%d probed" % (done, len(cands)), flush=True)
    finally:
        scraper.SESSION = orig

    # Sort: boards first (high-confidence before low), by job count desc; then the rest.
    def keyf(r):
        has = 1 if r.get("board_url") else 0
        hi = 1 if r.get("confidence") == "high" else 0
        jc = int(r["job_count"]) if str(r.get("job_count")).isdigit() else 0
        return (-has, -hi, -jc, r["employer"].lower())
    results.sort(key=keyf)

    cols = ["employer", "dba", "size", "state", "sites", "career_page",
            "ats_type", "job_count", "board_url", "confidence"]
    with open(REPORT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in cols})

    hits = [r for r in results if r.get("board_url")]
    hi = [r for r in hits if r.get("confidence") == "high"]
    by_ats = {}
    for r in hits:
        by_ats[r["ats_type"]] = by_ats.get(r["ats_type"], 0) + 1
    cp_only = sum(1 for r in results if not r.get("board_url") and r.get("career_page"))
    print("\n=== %d probed -> %d boards (%d high-confidence), %d careers-page-only, %d nothing ==="
          % (len(results), len(hits), len(hi), cp_only, len(results) - len(hits) - cp_only))
    print("by ATS:", ", ".join("%s %d" % (a, n) for a, n in sorted(by_ats.items(), key=lambda x: -x[1])))
    print("report written:", os.path.abspath(REPORT))


if __name__ == "__main__":
    main()
