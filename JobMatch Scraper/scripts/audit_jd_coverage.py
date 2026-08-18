#!/usr/bin/env python3
"""audit_jd_coverage.py — why does the site say "description pending"?

"Some JDs are missing" is not an actionable statement. This splits the backlog into the three
things it can actually be, because each has a different answer:

  REPAIRABLE   the description is in jd_cache.json.gz but not in the `jd` column. Nothing to
               fetch — score_jobs' reconciliation pass writes these from the cache. If this
               number is large, the two stores have drifted and something is dropping writes.
  BLOCKED      the host refuses a server-side fetch (403 bot-wall, 405 WAF challenge) or the
               posting is gone (404, Workday's "not found: Job_Posting_Anchor_ID"). Not a bug;
               the prune evicts these. Chasing them is how a fetch budget gets wasted.
  EXTRACTABLE  we get a 200 with real bytes and still pull no text. THIS is the fixable class —
               it means the host needs a detail-JD extractor it doesn't have yet.
  THIN         the row HAS a description, and it is a JavaScript loading shell or a page title
               plus a nav bar. Invisible to everything above, because those all start from
               "the jd column is empty" — which is exactly why 1,533 rows (7.0% of the corpus)
               sat in this state unnoticed while coverage was reported at 97.4%. Scores 0 and
               reads blank in the feed, identically to an empty one.

Read-only: it fetches sampled URLs and writes a CSV. It changes nothing.

    python scripts/audit_jd_coverage.py                    # 2 urls per host
    python scripts/audit_jd_coverage.py --per-host 5 --workers 16
    python scripts/audit_jd_coverage.py --host-filter workday   # one ATS family
"""
import argparse
import collections
import concurrent.futures
import csv
import os
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db
import scraper
import scraper.score_jobs as sj

REPORT = "jd_coverage_audit.csv"


def _classify(rec):
    """Which of the three buckets this probe result belongs in."""
    if rec["detail_jd"] > 200:
        return "extractable-ok"
    st = str(rec["status"])
    if st in ("403", "405") or st.startswith("ERR"):
        return "blocked"
    if st in ("404", "410"):
        return "gone"
    if rec["bytes"] < 500:
        return "gone"                       # 200 with an empty body is a closed posting
    return "extractable-MISSING"            # real page, no extractor got text out of it


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-host", type=int, default=2)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--host-filter", default="")
    ap.add_argument("--out", default=REPORT)
    ap.add_argument("--thin-hosts", type=int, default=14,
                    help="how many thin hosts to list (they are the extractor to-do list)")
    args = ap.parse_args()

    rows = db.load_jobs(cols="url")
    all_urls = {r["url"] for r in rows if r.get("url")}
    missing = db.urls_missing_jd() & all_urls

    # The repairable split needs no network at all, so report it before spending any.
    cached = sj._load_jd_cache()
    repairable = missing & set(cached)
    print("corpus %d rows | %d with no description in the database (%.1f%%)"
          % (len(all_urls), len(missing), 100.0 * len(missing) / max(len(all_urls), 1)))
    print("  REPAIRABLE (already in jd_cache.json.gz, just never written): %d" % len(repairable))
    print("  needs a fetch: %d" % len(missing - repairable))
    if repairable:
        print("  -> run `python -m scraper.score_jobs --new-only`; it writes these with no fetch.")

    # THE OTHER HALF OF THE BACKLOG, and it costs no network either. A row holding a shell is
    # indistinguishable from an empty one in the feed, but every count above starts from "jd is
    # empty" and therefore cannot see it. Read from the cache, which mirrors the column for
    # these rows (nothing writes a thin value; score_jobs' reconciliation keeps the two in step).
    thin = {u: len((cached.get(u) or "").strip()) for u in all_urls
            if u not in missing and 0 < len((cached.get(u) or "").strip()) < core._MIN_JD_CHARS}
    if thin:
        print("\n  THIN (holds a shell, not a description; scores 0 exactly like an empty one):"
              " %d" % len(thin))
        by_thin = collections.Counter(urlsplit(u).netloc for u in thin)
        for h, n in by_thin.most_common(args.thin_hosts):
            sample = min((u for u in thin if urlsplit(u).netloc == h), key=len)
            print("     %-42s %5d   e.g. %d chars" % (h[:42], n, thin[sample]))
        print("  -> `python scripts/refetch_thin_jds.py --host <h>` for one host now, or let the"
              "\n     scorer's own bounded per-host probe reach it (SCORE_THIN_PROBE).")
    print("\n  usable descriptions: %d of %d (%.1f%%)"
          % (len(all_urls) - len(missing) - len(thin), len(all_urls),
             100.0 * (len(all_urls) - len(missing) - len(thin)) / max(len(all_urls), 1)))

    todo = sorted(missing - repairable)
    if args.host_filter:
        todo = [u for u in todo if args.host_filter.lower() in u.lower()]

    by_host = collections.defaultdict(list)
    for u in todo:
        by_host[urlsplit(u).netloc].append(u)

    sample = []
    for host, us in sorted(by_host.items(), key=lambda kv: -len(kv[1])):
        sample += [(host, len(us), u) for u in us[:args.per_host]]
    print("\nprobing %d url(s) across %d host(s)...\n" % (len(sample), len(by_host)))

    def probe(item):
        host, n, u = item
        rec = {"host": host, "host_backlog": n, "url": u, "status": "", "bytes": 0,
               "detail_jd": 0, "page_text": 0}
        try:
            r = scraper._safe_get(u, timeout=20)
            rec["status"], rec["bytes"] = r.status_code, len(r.text or "")
        except Exception as e:
            rec["status"] = "ERR:" + type(e).__name__
        try:
            rec["detail_jd"] = len(sj.detail_jd(u)[1] or "")
        except Exception:
            pass
        try:
            rec["page_text"] = len(core.fetch_jd(u) or "")
        except Exception:
            pass
        rec["verdict"] = _classify(rec)
        return rec

    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for rec in ex.map(probe, sample):
            out.append(rec)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["host", "host_backlog", "verdict", "status",
                                          "bytes", "detail_jd", "page_text", "url"])
        w.writeheader()
        w.writerows(out)

    # Weight each host by its BACKLOG, not by how many of it we sampled — two probes of a host
    # holding 248 rows and two of a host holding 2 are not the same finding.
    print("%-42s %8s  %-20s %s" % ("HOST", "BACKLOG", "VERDICT", "status"))
    agg = collections.defaultdict(list)
    for r in out:
        agg[r["host"]].append(r)
    weighted = collections.Counter()
    for host, recs in sorted(agg.items(), key=lambda kv: -kv[1][0]["host_backlog"]):
        best = max(recs, key=lambda r: r["detail_jd"])
        weighted[best["verdict"]] += recs[0]["host_backlog"]
        print("%-42s %8d  %-20s %s" % (host[:42], recs[0]["host_backlog"], best["verdict"],
                                       ",".join(sorted({str(r["status"]) for r in recs}))))

    print("\nBacklog by verdict (rows, not samples):")
    for v, n in weighted.most_common():
        print("  %-22s %6d" % (v, n))
    print("\nWrote %s" % args.out)
    if weighted.get("extractable-MISSING"):
        print("\n%d row(s) sit on hosts that answer 200 with real content and still yield no "
              "text.\nThose are the ones worth writing an extractor for."
              % weighted["extractable-MISSING"])


if __name__ == "__main__":
    main()
