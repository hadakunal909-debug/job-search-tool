"""SLICING MUST NOT CHANGE WHAT IS STORED.

main() sweeps in slices when SCRAPE_SLICE is set, writing and releasing each slice instead of
holding the whole corpus for one final db.add_jobs(). That exists because two cPanel runs were
SIGKILLed on 2026-08-24 having banked nothing -- the second after a COMPLETE 48.4-minute sweep.

The risk the split introduces is a DIFFERENT CORPUS, not a crash: per-slice re-initialisation of
an accumulator that should span the run (the tally, the fingerprint list, cross-slice URL dedupe)
would silently admit or drop rows. So this asserts on the ROWS ACTUALLY WRITTEN -- the union
across every add_jobs call -- rather than on a log line or a return value.
"""
import os, sys, json

os.environ["EV_OFF"] = "1"          # analytics reads this once at import (analytics.py:33)
os.environ["DISCOVER_LIMIT"] = "0"  # else main() probes ~60 real employers over the network
os.environ.setdefault("DB_REQUIRE", "")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scraper
from scraper import db as _db

# A SOURCES entry is the tuple (url, ats_type, company).
BOARDS = [("https://x.test/%02d" % i, "greenhouse", "Co%02d" % i) for i in range(10)]

def _postings(entry):
    """Three postings per board: one plainly ours, one plainly not, one non-US."""
    c = entry[2]
    return [
        {"title": "Software Engineer", "company": c, "location": "Boston, MA",
         "url": "https://x.test/%s/1" % c, "found_date": "", "jd": ""},
        {"title": "Line Cook", "company": c, "location": "Boston, MA",
         "url": "https://x.test/%s/2" % c, "found_date": "", "jd": ""},
        {"title": "Data Analyst", "company": c, "location": "Bengaluru, India",
         "url": "https://x.test/%s/3" % c, "found_date": "", "jd": ""},
    ]

def run(slice_size):
    """Run main() with the network and the database stubbed; return the rows it wrote."""
    written, jds = [], {}

    def fake_scrape_all(sources, workers=None, progress=None, board_results=None, budget_min=None):
        out = []
        for e in sources:
            rows = _postings(e)
            out.extend(rows)
            if board_results is not None:
                board_results.append({"entry": e, "company": e[2], "ok": True,
                                      "skipped": False, "err": None, "secs": 0.1,
                                      "urls": {r["url"] for r in rows}})
            if progress:
                progress(len(board_results or []), len(sources), len(out))
        return out

    stubs = {
        "scrape_all": fake_scrape_all,
        "custom_sources": lambda *a, **k: [],
        "load_sponsors": lambda *a, **k: set(),
        "fill_missing_jds": lambda *a, **k: (0, 0),
        "reconcile_closed": lambda *a, **k: (0, 0),
        "save_board_health": lambda *a, **k: None,
        "truncation_report": lambda *a, **k: "",
    }
    dbstubs = {
        "add_jobs": lambda rows: written.append([dict(r) for r in rows]),
        "update_jds": lambda m: jds.update(m),
        "existing_urls": lambda *a, **k: set(),
        "blocked_company_keys": lambda *a, **k: set(),
        "load_jobs": lambda *a, **k: [],
        "prune_old_jobs": lambda *a, **k: 0,
        "stale_urls": lambda *a, **k: [],
        "set_scrape_status": lambda *a, **k: None,
        "using_supabase": lambda: True,
        "backend_name": lambda: "stub",
    }
    old = {k: getattr(scraper, k, None) for k in stubs}
    oldb = {k: getattr(_db, k, None) for k in dbstubs}
    old_sources = scraper.SOURCES
    old_slice = scraper.SCRAPE_SLICE
    old_rotate = scraper.SCRAPE_ROTATE
    try:
        for k, v in stubs.items():
            setattr(scraper, k, v)
        for k, v in dbstubs.items():
            setattr(_db, k, v)
        scraper.SOURCES = list(BOARDS)
        scraper.SCRAPE_SLICE = slice_size
        scraper.SCRAPE_ROTATE = 0          # rotation would reorder the list, not the SET
        scraper.main()
    finally:
        for k, v in old.items():
            if v is not None:
                setattr(scraper, k, v)
        for k, v in oldb.items():
            if v is not None:
                setattr(_db, k, v)
        scraper.SOURCES = old_sources
        scraper.SCRAPE_SLICE = old_slice
        scraper.SCRAPE_ROTATE = old_rotate
    return written

def urls(batches):
    return sorted(r["url"] for b in batches for r in b)

fails = []

one = run(0)             # single pass, the old behaviour
many = run(3)            # 10 boards in slices of 3 -> 4 slices

if len(one) > 1:
    fails.append("SCRAPE_SLICE=0 should write once, wrote %d times" % len(one))
if len(many) < 2:
    fails.append("SCRAPE_SLICE=3 over 10 boards should write more than once, wrote %d" % len(many))
if urls(one) != urls(many):
    a, b = set(urls(one)), set(urls(many))
    fails.append("sliced run stored a DIFFERENT corpus: only-unsliced=%s only-sliced=%s"
                 % (sorted(a - b)[:5], sorted(b - a)[:5]))
if len(urls(many)) != len(set(urls(many))):
    fails.append("sliced run wrote the same url twice")
# the whole point: work is banked as it goes, not at the end
if len(many) < 4:
    fails.append("expected one write per slice (4), got %d" % len(many))
# and the run's own summary file must describe the WHOLE run, not the last slice
try:
    saved = json.load(open("last_new_jobs.json", encoding="utf-8"))
    if len(saved) != len(urls(many)):
        fails.append("last_new_jobs.json holds %d row(s), the run stored %d"
                     % (len(saved), len(urls(many))))
except Exception as e:
    fails.append("last_new_jobs.json unreadable: %s" % e)

print("unsliced: %d write(s), %d row(s)" % (len(one), len(urls(one))))
print("sliced:   %d write(s), %d row(s)" % (len(many), len(urls(many))))
if fails:
    print("\nFAIL")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("\nok - slicing stores exactly what one pass stores, and banks it per slice")
