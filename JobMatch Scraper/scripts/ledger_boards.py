#!/usr/bin/env python3
"""
ledger_boards.py — recover boards from the URLs the sweep already banked.

WHY THIS EXISTS. jobspy_sweep probes for a board by GUESSING a slug from the company name
(careers.<slug>.com / jobs.<slug>.com and four JSON APIs). Four passes of that have returned
6.0%, 7.5%, 7.3% and 5.1%, and the 2026-09-14 pass produced 15 boards carrying 10 H-1B filings
between them while the 91 employers it could only find a careers page for carried 50,197.

Meanwhile the answer was already in the ledger. Indeed's `job_url_direct` is the EMPLOYER'S OWN
ATS link, and scraper._jobspy_url prefers it, so 93% of the indeed rows in `jobspy_findings`
store a real board URL rather than an aggregator one. Measured 2026-09-14 over 4,119 rows:
`scraper.detect_board` resolves a normalised board URL and an ATS type for 177 employers, 165 of
them not already scraped, with no guessing and no network.

This does NOT adopt anything. It writes a probe-result-shaped CSV so the existing chain can:

    python scripts/ledger_boards.py --out ledger_boards_2026-09-14.csv
    python -m scraper.adopt_everify_boards --csv ledger_boards_2026-09-14.csv --dry-run
    python scripts/review_candidates.py --csv ledger_boards_2026-09-14.csv
    python -m scraper.adopt_everify_boards --csv ledger_boards_2026-09-14.csv \
        --added-by ledger-url:2026-09-14

LinkedIn and jobright rows cannot contribute: both serve every posting on their own domain, so
there is no direct URL to read. That is two thirds of the ledger, and it is why this recovers
7.8% of the unscraped employers and not all of them.
"""
import argparse
import collections
import concurrent.futures
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db
import scraper
from scraper.probe_everify_candidates import COLS

# Greenhouse's own shortener. detect_board cannot read it -- the slug is in the redirect, not
# the path -- and it is 16 employers / 298 filings in the ledger, so it is worth the one GET.
_SHORTENERS = ("grnh.se",)


def _norm(s):
    """The sweep's own normaliser, so 'already scraped' means the same thing here as there."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _canon(url):
    """Board identity is host + path. A multi-tenant ATS host is shared BY DESIGN, so comparing
    hosts marks every greenhouse/ashby/lever/smartrecruiters hit as already-held and is always
    wrong."""
    return re.sub(r"^https?://(www\.)?", "", (url or "").lower().strip().rstrip("/"))


def _follow(url):
    """Resolve a shortener to whatever it points at. Left alone on any failure."""
    if not any(h in core.url_host(url) for h in _SHORTENERS):
        return url
    try:
        r = scraper.SESSION.get(url, headers=scraper.HEADERS, timeout=15, allow_redirects=True)
        return r.url or url
    except Exception:
        return url


def held_boards():
    """(board urls, names) for everything we already scrape.

    Two normalisers, because neither alone is enough: the sweep's `_norm` misses "Ford Motor
    Company" against SOURCES' "Ford Motor", and core.norm_company collapses some distinct firms
    onto a big-name key. Union them for an EXCLUSION, where a false positive only costs a board
    we already have.
    """
    names = set()
    urls = {_canon(u) for u, _k, _n in scraper.SOURCES}
    for _u, _k, n in scraper.SOURCES:
        names |= {_norm(n), core.norm_company(n)}
    try:
        for b in (db.list_boards() or []):
            urls.add(_canon(b.get("url")))
            names |= {_norm(b.get("company")), core.norm_company(b.get("company") or "")}
    except Exception:
        pass
    return urls, names


def fold_ledger(rows):
    """One record per employer: is it new, its best direct URL, its filing count.

    The first direct URL wins rather than the newest. They are all the same employer's board;
    picking one is enough and re-picking per row would just re-run detect_board.
    """
    by = collections.OrderedDict()
    for r in rows:
        name = (r.get("company") or "").strip()
        if not name:
            continue
        g = by.setdefault(name, {"company": name, "new": False, "url": "", "filings": 0,
                                 "routes": "", "postings": 0, "sources": set()})
        g["postings"] += 1
        g["new"] = g["new"] or bool(r.get("company_is_new"))
        if r.get("source"):
            g["sources"].add(r["source"])
        if r.get("visa_routes") and not g["routes"]:
            g["routes"] = r["visa_routes"]
        try:
            g["filings"] = max(g["filings"], int(r.get("h1b_filings") or 0))
        except (TypeError, ValueError):
            pass
        url = (r.get("url") or "").strip()
        if url and not g["url"] and not core.is_aggregator_url(url):
            g["url"] = url
    return list(by.values())


def resolve(rec, held_urls, held_names):
    """(row, reason) — a probe-result-shaped row, or None and why not."""
    if not rec["new"]:
        return None, "not flagged new"
    if _norm(rec["company"]) in held_names or core.norm_company(rec["company"]) in held_names:
        return None, "already scraped"
    if not rec["url"]:
        return None, "no direct url (linkedin/jobright only)"
    url = _follow(rec["url"])
    hit = None
    try:
        hit = scraper.detect_board(url)
    except Exception:
        hit = None
    if not hit:
        # detect_board is pure string parsing by contract, so it cannot read a Paylocity link:
        # /Recruiting/Jobs/Details/<id> carries no company GUID and detect_paylocity has to fetch
        # the page to find it. We already fetch every board to count it, so the extra request is
        # affordable HERE and is not in the sweep. `paylocity` has been one of the 40 adapters all
        # along, so these 31 employers read as unscrapeable only because nothing asked.
        try:
            hit = scraper.detect_paylocity(url)
        except Exception:
            hit = None
    if not hit:
        return None, "direct url, but no adapter reads %s" % (core.url_host(rec["url"]) or "?")
    burl, ats = hit[0], hit[1]
    if _canon(burl) in held_urls:
        return None, "board already scraped"
    # 'high' is what grade()'s second branch reads, and it is honest here for the same reason it
    # is honest for a careers.<co>.com hit: this URL is not a construction. It came off ONE
    # posting that the aggregator attributed to THIS employer, which is a per-posting witness, not
    # a name guess.
    #
    # Do NOT try to corroborate it against the slug. Tenants are acronyms and opaque codes by
    # design -- Arizona State is `asu.wd1`, Schweitzer Engineering Laboratories is `selinc`,
    # Berkeley Research Group is `thinkbrg`, Boise Cascade is ultipro's `BOI1001BOIS` -- so a
    # name-similarity test marks 65 of 154 correct boards as unproven. Nor is there a name
    # endpoint to ask: Workday is 70 of these and its board pages are SPA shells with no <title>,
    # while /wday/cxs/<tenant>/<site> answers 200 with non-JSON.
    #
    # What remains unverified is the aggregator's attribution -- a staffing firm posting a
    # client's role carries the CLIENT's board. That is what the bodyshop flag, the admin
    # blocklist and review_candidates.py are for, and greenhouse/smartrecruiters rows still get
    # the real name check because grade() runs it first and it can veto this.
    row = dict.fromkeys(COLS, "")
    row.update({"employer": rec["company"], "bucket": "ledger-url",
                "category": ",".join(sorted(rec["sources"])), "h1b_filings": rec["filings"] or 0,
                "bodyshop": "yes" if (core.is_agency(rec["company"])
                                      or core.BODYSHOP_RE.search(rec["company"])) else "no",
                "career_page": "yes", "ats_type": ats, "job_count": "",
                "board_url": burl, "confidence": "high"})
    return row, ""


def main():
    p = argparse.ArgumentParser(description="Recover boards from jobspy_findings' own URLs.")
    p.add_argument("--out", default="ledger_boards.csv")
    p.add_argument("--all", action="store_true",
                   help="include employers the sweep did not flag as new")
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--no-count", action="store_true",
                   help="skip the posting count (leaves adopt's yield gate disabled)")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()

    rows = db.list_findings() or []
    if not rows:
        print("ledger is empty or unreadable — is DB_REQUIRE/DB_PROXY_* set?")
        return 1
    print("ledger: %d posting row(s)" % len(rows))

    held_urls, held_names = held_boards()
    print("already scraped: %d board url(s)" % len(held_urls))

    recs = fold_ledger(rows)
    print("employers: %d" % len(recs))

    out, why = [], collections.Counter()
    for rec in recs:
        if a.all:
            rec["new"] = True
        row, reason = resolve(rec, held_urls, held_names)
        if row is None:
            why[reason] += 1
            continue
        # Two employers can legitimately share one board (Roush / Roush Industries). Keep the
        # first; the adopter would drop the second on its own `seen` set anyway.
        if any(_canon(r["board_url"]) == _canon(row["board_url"]) for r in out):
            why["shares a board with an employer already in this batch"] += 1
            continue
        out.append(row)
        if a.verbose:
            print("  %-34s %-16s %s" % (row["employer"][:34], row["ats_type"], row["board_url"][:58]))

    # COUNT THE POSTINGS, because adopt_everify_boards' yield check is gated on job_count >= 500
    # (relevance_yield, adopt_everify_boards.py:100-106) and reads a blank as 0 -- so shipping
    # these rows without a count would silently disable the one gate that rejects a board whose
    # title filter keeps nothing. Same call the name probe makes, just on a URL we already have.
    if not a.no_count and out:
        print("\ncounting postings behind %d board(s)…" % len(out))
        def count(row):
            try:
                return row, scraper.probe_board(row["board_url"], row["ats_type"])
            except Exception:
                return row, 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
            for row, n in ex.map(count, out):
                row["job_count"] = 0 if n is None else n
        # Drop what reads nothing, here rather than downstream: probe_board answers 0 for
        # "reachable but empty" and None for "could not read", and the adopter's yield gate only
        # looks at boards claiming 500+ postings, so neither would be caught there. This is the
        # same bar find_everify_boards.discover() applies before it accepts a board at all.
        dead = [r for r in out if not int(r["job_count"] or 0)]
        out = [r for r in out if int(r["job_count"] or 0)]
        why["board reads 0 postings today"] += len(dead)
        print("  dropped %d board(s) the scraper reads nothing off" % len(dead))

    out.sort(key=lambda r: (-int(r["h1b_filings"] or 0), r["employer"].lower()))
    with open(a.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        for r in out:
            w.writerow(r)

    print("\nnot resolved:")
    for reason, n in why.most_common():
        print("   %-52s %5d" % (reason, n))
    print("\nwrote %s — %d board(s)" % (a.out, len(out)))
    print("by ATS: %s" % dict(collections.Counter(r["ats_type"] for r in out).most_common()))
    print("with certified H-1B filings: %d" % sum(1 for r in out if int(r["h1b_filings"] or 0)))
    print("confidence: %s" % dict(collections.Counter(r["confidence"] for r in out)))
    print("\nnext: python -m scraper.adopt_everify_boards --csv %s --dry-run" % a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
