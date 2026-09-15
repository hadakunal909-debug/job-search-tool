#!/usr/bin/env python3
"""web.get_jobs' three-level cache, counted in FULL CORPUS READS.

Every assertion here is about one number: how many times get_jobs() went to the network for the
whole jobs table. At ~20k rows that call is ~13 MB, it is the single largest thing the web app
can ask Supabase for, and the free tier meters egress — a regression here does not break a test
or slow a page down, it quietly spends the month's allowance and says nothing. That is exactly
how it went unnoticed before: the 2026-08-14 overage was ~650 MB/day against a projected ~115,
and part of it was this function re-reading rows that had not changed.

The case that matters is a COLD WORKER WITH A STALE SNAPSHOT. Passenger recycles workers freely
and the corpus moves only on the two weekday scrapes, so "a fresh process, an old-but-correct
snapshot, and a corpus that has not moved" is the app's normal state, not an edge case. The
fingerprint probe (a HEAD plus a one-row select — see db.jobs_fingerprint) can answer "has it
moved?" for nothing, and the snapshot stores the fingerprint it was written with, so nothing
about that situation needs a re-read. It used to cost one anyway, because only a worker that
already held rows in memory was allowed to revalidate.

    python scripts/test_jobs_cache.py
"""
import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Must be set BEFORE web is imported: _JOBS_SNAPSHOT is resolved at module scope, and the point
# of the env var is that a read-only deploy can relocate it. Pointing it at a temp file also
# keeps this test off the real snapshot, which the local dev server shares.
_SNAP = os.path.join(tempfile.gettempdir(), "test_jobs_snapshot_%d.json.gz" % os.getpid())
os.environ["JOBS_SNAPSHOT"] = _SNAP

import db
import web

ROWS = [{"url": "https://example.com/%d" % i, "title": "Engineer %d" % i,
         "company": "Acme", "location": "Boston, MA"} for i in range(5)]
FP = (5, "2026-08-13T00:00:00Z")

fails = []
reads = {"n": 0}
ran = []


def check(label, ok):
    ran.append(label)
    print("  %s  %s" % ("ok " if ok else "FAIL", label))
    if not ok:
        fails.append(label)


def install(fp=FP, rows=None):
    """Point db at counted stubs. Nothing in this file may touch a real database — see the
    egress note above, and CI has no credentials at all."""
    db.load_jobs = lambda include_jd=True, cols=None: (reads.__setitem__("n", reads["n"] + 1)
                                                       or list(rows if rows is not None else ROWS))
    db.jobs_fingerprint = lambda: fp


def cold_worker():
    """A freshly recycled Passenger process: no rows in memory, snapshot untouched on disk."""
    web._jobs_cache["rows"] = None
    web._jobs_cache["fp"] = None
    web._jobs_cache["at"] = 0


def age_snapshot(seconds):
    os.utime(_SNAP, (time.time() - seconds, time.time() - seconds))


print("=" * 72)
print("get_jobs: full corpus reads")
print("=" * 72)

install()
try:
    os.remove(_SNAP)
except OSError:
    pass

cold_worker()
web.get_jobs()
check("cold start with no snapshot reads once", reads["n"] == 1)

web.get_jobs()
check("a warm worker inside the TTL does not read again", reads["n"] == 1)

cold_worker()
web.get_jobs()
check("a second cold worker is served by the fresh snapshot", reads["n"] == 1)

# THE REGRESSION TEST. Older than the TTL, so neither the memory cache nor the fresh-snapshot
# path applies -- but the corpus has not moved, and the snapshot knows the fingerprint it was
# written with. This is the case that used to cost a full read every time.
cold_worker()
age_snapshot(2 * web._JOBS_TTL)
web.get_jobs()
check("a cold worker revalidates a STALE snapshot instead of re-reading", reads["n"] == 1)
check("...and the rows it serves are the real ones", len(web.get_jobs()) == len(ROWS))

# Revalidating must also refresh the file, or every later worker repeats the probe.
check("...and it refreshes the snapshot for the next worker",
      time.time() - os.path.getmtime(_SNAP) < web._JOBS_TTL)

# A MOVED CORPUS MUST STILL COST A READ. The whole scheme rests on the fingerprint being
# believed, so the failure that matters is not "too many reads", it is serving yesterday's jobs
# forever because a changed corpus looked unchanged.
cold_worker()
age_snapshot(2 * web._JOBS_TTL)
install(fp=(6, "2026-08-14T00:00:00Z"), rows=ROWS + [{"url": "https://example.com/new",
                                                      "title": "New", "company": "Acme"}])
web.get_jobs()
check("a changed fingerprint forces the re-read", reads["n"] == 2)
check("...and the new row is visible", len(web.get_jobs()) == len(ROWS) + 1)

# An UNAVAILABLE probe means "don't know", which must read rather than serve stale rows.
cold_worker()
age_snapshot(2 * web._JOBS_TTL)
install(fp=(None, ""))
web.get_jobs()
check("an unavailable fingerprint probe re-reads rather than guessing", reads["n"] == 3)

# Beyond the cap, an old snapshot is not revalidated at all: jobs_fingerprint cannot see a
# PATCH (it is count + max first_seen, and update_job_fields moves neither), so a snapshot that
# has sat through a scoring run is refetched on age alone.
install()
cold_worker()
web.get_jobs()                                   # re-seed a snapshot at the current fingerprint
before = reads["n"]
cold_worker()
age_snapshot(web._SNAPSHOT_MAX_AGE + 60)
web.get_jobs()
check("a snapshot past _SNAPSHOT_MAX_AGE is re-read, probe or no probe",
      reads["n"] == before + 1)

# _invalidate_jobs is the extension's path: it changed rows and renders nothing, so it must
# force the NEXT render to re-read -- including past the snapshot, which it deletes.
before = reads["n"]
web._invalidate_jobs()
cold_worker()
web.get_jobs()
check("_invalidate_jobs forces a re-read on the next render", reads["n"] == before + 1)

# An edit to an existing posting changes only the database revision, not row/score counts.
install(fp=(5, "2026-08-13", 5, "100"), rows=[dict(r, exp_max_years=5) for r in ROWS])
web.get_jobs(force=True)
age_snapshot(2 * web._JOBS_TTL)
web._jobs_cache["at"] = 0
install(fp=(5, "2026-08-13", 5, "101"), rows=[dict(r, exp_max_years=2) for r in ROWS])
check("corrected experience reaches an already-warm worker",
      all(r["exp_max_years"] == 2 for r in web.get_jobs()))
check("data updates are revalidated within one minute", web._JOBS_TTL <= 60)

try:
    os.remove(_SNAP)
except OSError:
    pass

print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all good - %d checks" % len(ran))
