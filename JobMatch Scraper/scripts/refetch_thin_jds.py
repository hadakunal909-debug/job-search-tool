#!/usr/bin/env python3
"""refetch_thin_jds.py — repair job rows whose stored JD is junk rather than absent.

audit_jd_coverage.py only looks at rows where `jd` is EMPTY. It cannot see the other failure:
a row that HAS a description which is really a JavaScript loading shell — "Loading … Sorry to
interrupt CSS Error Refresh" (Salesforce Lightning), "You need to enable JavaScript to run this
app." (JobDiva), or just the page title and nav. Those rows score 0 and read as blank in the
feed, and because score_jobs is incremental — it queues a fetch only when `jd` is empty — a junk
value is STICKY. Nothing ever tries again.

Measured 2026-08-17: 2,154 rows (9.8% of the corpus) were in that state, and every one scored 0.

This is deliberately NOT a change to score_jobs' queue. Treating every thin JD as missing would
make each run spend its whole fetch budget retrying the ~2,100 rows whose host genuinely cannot
be read server-side, which is the trap audit_jd_coverage.py's BLOCKED class exists to prevent.
Run this instead, per host, once an extractor for that host actually works.

    python scripts/refetch_thin_jds.py                        # dry run, all hosts
    python scripts/refetch_thin_jds.py --host michaelpage.com
    python scripts/refetch_thin_jds.py --host michaelpage.com --apply
    python scripts/refetch_thin_jds.py --under 400 --min-gain 3
"""
import os
import sys
import argparse
import collections
import concurrent.futures
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db
import scraper.score_jobs as sj
from scraper.score_jobs import detail_jd

# ONE definition of "thin", shared with the scorer (core._MIN_JD_CHARS, 400). It used to be a
# second constant here, which is a number that can disagree with the one the feed and the scorer
# actually use. 400 is comfortably under the shortest real JD seen (Michael Page's genuine ones
# run 1,500+) and comfortably over the longest shell (Salesforce's is ~46, JobDiva's ~63).
THIN_UNDER = core._MIN_JD_CHARS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", action="append", default=[],
                    help="only this host suffix (repeatable)")
    ap.add_argument("--under", type=int, default=THIN_UNDER)
    ap.add_argument("--min-gain", type=float, default=3.0,
                    help="only write when the new JD is this many times longer")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--board", help="board URL to pull descriptions from in bulk, when the "
                                    "stored per-job URL cannot be read on its own")
    ap.add_argument("--board-ats", help="ats_type for --board (e.g. phenom)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    rows = db.load_jobs(["url", "jd", "company", "title"])
    thin = [r for r in rows
            if r.get("url") and (sj._is_thin_jd(r.get("jd")) if a.under == THIN_UNDER
                                 else ((r.get("jd") or "").strip()
                                       and len(r["jd"]) < a.under))]
    if a.host:
        thin = [r for r in thin
                if any(urlparse(r["url"]).netloc.endswith(h) for h in a.host)]
    if a.limit:
        thin = thin[:a.limit]

    by_host = collections.Counter(urlparse(r["url"]).netloc for r in thin)
    print("rows in table   : %d" % len(rows))
    print("thin (<%d chars): %d" % (a.under, len(thin)))
    for h, n in by_host.most_common(12):
        print("   %-40s %d" % (h, n))
    if not thin:
        return 0

    # BOARD MODE. Some rows can never be repaired one URL at a time, because the URL we store
    # is not a readable page: Actalent's rows are Salesforce Lightning apply links, and the
    # description only exists behind its Phenom board's jobDetail widget. score_jobs' own bulk
    # path cannot reach them either — its `missing` set is rows whose jd is EMPTY, and these
    # hold junk — so the board map has to be driven from here.
    board_map = {}
    if a.board:
        from scraper.score_jobs import jd_map_for
        want = {r["url"] for r in thin}
        print("\npulling descriptions from %s (%s) for %d needed url(s)..."
              % (a.board, a.board_ats, len(want)), flush=True)
        board_map = jd_map_for(a.board, a.board_ats, want) or {}
        print("board returned %d description(s)" % len(board_map))

    def work(r):
        jd = board_map.get(r["url"], "")
        if jd:
            return r, jd
        try:
            return r, (detail_jd(r["url"])[1] or "")
        except Exception:
            return r, ""

    fixed, still = {}, collections.Counter()
    print("\nre-fetching %d row(s) with %d workers..." % (len(thin), a.workers), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, (r, jd) in enumerate(ex.map(work, thin), 1):
            old = len(r["jd"])
            # A gain multiple, not an absolute length: it is what distinguishes "the extractor
            # now works" from "we re-read the same shell and it is 46 characters again".
            if jd and len(jd) >= old * a.min_gain and len(jd) >= 400:
                fixed[r["url"]] = jd
            else:
                still[urlparse(r["url"]).netloc] += 1
            if i % 200 == 0:
                print("   ... %d/%d" % (i, len(thin)), flush=True)

    print("\nrecovered  : %d" % len(fixed))
    print("still thin : %d" % sum(still.values()))
    for h, n in still.most_common(10):
        print("   %-40s %d  (host needs an extractor)" % (h, n))
    if fixed:
        avg = sum(len(v) for v in fixed.values()) // len(fixed)
        print("\naverage recovered length: %d chars" % avg)
        for u, jd in list(fixed.items())[:3]:
            print("   %s\n      %s..." % (u[-58:], jd[:100]))

    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0
    if fixed:
        db.update_jds(fixed)
        print("\nwrote %d JD(s) to the database." % len(fixed))
        # AND to the disk cache, or the repair does not take effect. There are two records of
        # "we have this description" — jd_cache.json.gz and the `jd` column — and score_jobs
        # reads the CACHE when deciding what text to analyse. Writing only the column leaves
        # the scorer tokenising the old shell, which is how 621 rows carrying 7,000-character
        # descriptions still scored 0 with jd_terms reading "css error", "interrupt css".
        from scraper.score_jobs import _load_jd_cache, _save_jd_cache
        bank = _load_jd_cache()
        bank.update(fixed)
        _save_jd_cache(bank)
        print("refreshed %d entry/entries in the on-disk JD cache." % len(fixed))
        # A repair by hand has to clear the scorer's per-host backoff, or the OTHER rows on the
        # host it just proved readable sit behind a 64-day timer that this run disproved. Failure
        # count to 0 and next=today marks the host hot, so the next heavy pass drains it.
        try:
            import datetime
            led = sj._load_thin_ledger()
            today = datetime.date.today().isoformat()
            hosts = {urlparse(u).netloc for u in fixed}
            for h in hosts:
                rec = led["hosts"].setdefault(h, {})
                rec.update({"f": 0, "ok": int(rec.get("ok") or 0) + 1, "next": today,
                            "last": today})
            sj._save_thin_ledger(led)
            print("cleared the retry backoff on %d host(s); the next heavy pass drains them."
                  % len(hosts))
        except Exception as e:
            print("  (could not update the thin-retry ledger: %s)" % str(e)[:80])
        print("\nNow run: python -m scraper.score_jobs   (recomputes jd_terms and rescores)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
