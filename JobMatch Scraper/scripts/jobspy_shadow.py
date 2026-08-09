#!/usr/bin/env python3
"""Measure what JobSpy WOULD add, without writing anything. Run this before enabling it.

The question this answers is not "does the library work" — it's "how much of what it returns is
a job we don't already have?" Every other number is downstream of that one. If a site returns
800 rows and 40 survive as net-new, it is not worth the block risk, and you should read that off
a report rather than discover it in the corpus a week later.

    python scripts/jobspy_shadow.py                          # the default site/phrase matrix
    python scripts/jobspy_shadow.py --sites indeed,google
    python scripts/jobspy_shadow.py --phrases "project manager|data engineer"
    python scripts/jobspy_shadow.py --results 25             # go easy while smoke-testing

There is deliberately NO --apply flag, not even a disabled one. This script reads the corpus and
the aggregators, prints, and exits; it is the one place in this change where that guarantee is
worth more than the convenience.

Run it LOCALLY first, then once in CI via workflow_dispatch. GitHub's runners egress from Azure
ranges that Cloudflare pre-flags as bot traffic, so a site that answers a laptop may refuse a
runner — and two different answers is itself the finding.

What to read, in order of how much it should change your mind:
  net new / returned   the whole case for the site. Small means don't bother.
  direct-url rate      how many rows arrive with the employer's own link, i.e. mergeable for
                       free. Glassdoor and ZipRecruiter are structurally 0 here.
  provable false merge a fingerprint collision where BOTH sides carry a distinct employer-direct
                       URL. Those are demonstrably two different openings, so a non-trivial
                       count here means the fingerprint filter must stay in log-only mode.
"""
import argparse
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db
import scraper


def _build_index(rows):
    """The same two structures main() builds: canonical urls, and posting fingerprints."""
    seen, fingerprints = set(), {}
    for r in rows:
        u = r.get("url") or ""
        if not u:
            continue
        seen.add(scraper.canonical_url(u).lower())
        k = core.posting_key(r.get("title"), r.get("company"), r.get("location"),
                             require_location=True)
        if k:
            fingerprints.setdefault(k, []).append(r)
    return seen, fingerprints


def _classify(job, seen, fingerprints, blocked, age_cutoff):
    """Replay main()'s filter chain IN THE SAME ORDER and name the bucket it lands in.

    Order matters as much as the checks do: the freshness gate runs before found_date is
    defaulted, and the fingerprint runs dead last, so a row reported as a duplicate here has
    already cleared everything else — exactly as it would in a real run.
    """
    job["url"] = scraper.canonical_url(job.get("url", ""))
    if not job["url"]:
        return "unusable url", None
    if job["url"].lower() in seen:
        return "already known (url)", None
    if blocked and db.block_key(job.get("company", "")) in blocked:
        return "blocked company", None
    keep, why = scraper.title_verdict(job.get("title") or "")
    if not keep:
        return ("off-target title" if why.startswith("off-target")
                else "no matching role keyword"), None
    if scraper.US_ONLY and not scraper.is_us_location(job.get("location", "")):
        return "non-US location", None
    if age_cutoff:
        posted = (job.get("found_date") or "")[:10]
        if posted and posted < age_cutoff:
            return "posted too long ago", None
    dupe = scraper.fingerprint_duplicate(job, fingerprints)
    if dupe:
        return "aggregator relist (fingerprint)", dupe
    return "NET NEW", None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", default="indeed,google,glassdoor,linkedin,zip_recruiter",
                    help="comma-separated JobSpy sites to probe")
    ap.add_argument("--phrases", default="|".join(scraper.JOBSPY_PHRASES),
                    help="pipe-separated search phrases")
    ap.add_argument("--location", default=scraper.JOBSPY_LOCATION)
    ap.add_argument("--results", type=int, default=scraper.JOBSPY_RESULTS,
                    help="results_wanted per query")
    ap.add_argument("--collisions", type=int, default=25,
                    help="how many fingerprint collisions to print side by side")
    a = ap.parse_args()

    sites = [s.strip().lower() for s in a.sites.replace(",", " ").split() if s.strip()]
    phrases = [p.strip() for p in a.phrases.split("|") if p.strip()]
    if not (sites and phrases):
        print("nothing to probe")
        return

    scraper.JOBSPY_RESULTS = a.results          # in-process only; nothing is persisted

    print("Reading the corpus (narrow select: %s)..." % db.COLS_DEDUPE)
    corpus = db.load_jobs(include_jd=False, cols=db.COLS_DEDUPE)
    seen, fingerprints = _build_index(corpus)
    blocked = db.blocked_company_keys()
    age_cutoff = ""
    if scraper.MAX_AGE_DAYS > 0:
        import datetime
        age_cutoff = (datetime.date.today()
                      - datetime.timedelta(days=scraper.MAX_AGE_DAYS)).isoformat()
    print("corpus rows          : %d" % len(corpus))
    print("canonical urls       : %d" % len(seen))
    print("posting fingerprints : %d" % len(fingerprints))
    print("probing %d site(s) x %d phrase(s), %d results each\n" % (len(sites), len(phrases),
                                                                    a.results))

    per_site = collections.defaultdict(lambda: collections.Counter())
    per_site_rows = collections.Counter()
    per_site_direct = collections.Counter()
    per_site_secs = collections.Counter()
    zero_returns = []
    collisions = []
    provable_false_merges = []

    for site in sites:
        for phrase in phrases:
            selector = "jobspy:%s|%s|%s" % (site, phrase, a.location)
            t0 = time.monotonic()
            try:
                rows = scraper.scrape_jobspy(selector)
            except Exception as e:
                per_site[site]["ERROR"] += 1
                print("  FAIL %-14s %-28s %s" % (site, phrase[:28], str(e)[:70]))
                continue
            secs = time.monotonic() - t0
            per_site_secs[site] += secs
            per_site_rows[site] += len(rows)
            if not rows:
                zero_returns.append((site, phrase))
                print("  ZERO %-14s %-28s %5.1fs  <- blocked, or nothing new?"
                      % (site, phrase[:28], secs))
                continue

            direct = sum(1 for r in rows if not core.is_aggregator_url(r.get("url") or ""))
            per_site_direct[site] += direct
            local = collections.Counter()
            for r in rows:
                bucket, dupe_url = _classify(dict(r), seen, fingerprints, blocked, age_cutoff)
                local[bucket] += 1
                per_site[site][bucket] += 1
                if dupe_url:
                    incumbent = next(
                        (x for x in fingerprints.get(
                            core.posting_key(r.get("title"), r.get("company"),
                                             r.get("location"), require_location=True), [])
                         if (x.get("url") or "") == dupe_url), {})
                    collisions.append((site, r, incumbent))
                    # BOTH sides carry a distinct employer-direct URL -> demonstrably two
                    # different openings, and suppressing one would be a real loss.
                    if (not core.is_aggregator_url(r.get("url") or "")
                            and not core.is_aggregator_url(dupe_url)
                            and scraper.canonical_url(r["url"]) != scraper.canonical_url(dupe_url)):
                        provable_false_merges.append((r, incumbent))
            print("  ok   %-14s %-28s %5.1fs  %3d rows  %3d direct  %3d net-new"
                  % (site, phrase[:28], secs, len(rows), direct, local["NET NEW"]))

    print("\n" + "=" * 78)
    print("PER SITE")
    print("=" * 78)
    hdr = "%-14s %6s %7s %8s %8s %9s %7s" % ("site", "rows", "direct", "url-dup",
                                             "fp-dup", "filtered", "NETNEW")
    print(hdr)
    print("-" * 78)
    for site in sites:
        c = per_site[site]
        rows = per_site_rows[site]
        filtered = (c["off-target title"] + c["no matching role keyword"]
                    + c["non-US location"] + c["posted too long ago"]
                    + c["blocked company"] + c["unusable url"])
        print("%-14s %6d %6d%% %8d %8d %9d %7d"
              % (site, rows,
                 (100 * per_site_direct[site] // rows) if rows else 0,
                 c["already known (url)"], c["aggregator relist (fingerprint)"],
                 filtered, c["NET NEW"]))
        if rows:
            print("%-14s %s" % ("", "net new / returned = %.1f%%   (%.1fs total)"
                               % (100.0 * c["NET NEW"] / rows, per_site_secs[site])))
    print("-" * 78)

    if zero_returns:
        print("\nZERO-ROW QUERIES (%d) — a silent 0 and a block look identical from here:"
              % len(zero_returns))
        for site, phrase in zero_returns[:20]:
            print("   %-14s %s" % (site, phrase))
        print("   NOTE: Google has returned 0 and ZipRecruiter 403 upstream since Sept 2025")
        print("         (JobSpy issue #302). Re-check from a runner before blaming the query.")

    print("\nPROVABLE FALSE MERGES: %d" % len(provable_false_merges))
    print("  (fingerprint collisions where BOTH sides carry a distinct employer-direct URL, so")
    print("   they are demonstrably two different openings. Non-trivial => keep the fingerprint")
    print("   filter in log-only mode: JOBSPY_FINGERPRINT_ENFORCE unset.)")
    for cand, inc in provable_false_merges[:10]:
        print("   %-40s" % (cand.get("title") or "")[:40])
        print("      candidate: %s" % (cand.get("url") or "")[:88])
        print("      incumbent: %s" % (inc.get("url") or "")[:88])

    if collisions:
        print("\nFINGERPRINT COLLISIONS — top %d, side by side."
              % min(a.collisions, len(collisions)))
        print("Read these. They are what decides whether the filter is safe to enforce.")
        by_host = collections.Counter(core.url_host(i.get("url") or "")
                                      for _, _, i in collisions)
        print("\n  incumbent host breakdown:")
        for host, n in by_host.most_common(12):
            print("     %-44s %5d" % (host[:44] or "?", n))
        print()
        for site, cand, inc in collisions[:a.collisions]:
            print("  [%s] %s | %s | %s" % (site, (cand.get("title") or "")[:38],
                                           (cand.get("company") or "")[:22],
                                           (cand.get("location") or "")[:22]))
            print("      would store: %s" % (cand.get("url") or "")[:92])
            print("      already have: %s" % (inc.get("url") or "")[:92])

    total_rows = sum(per_site_rows.values())
    total_new = sum(per_site[s]["NET NEW"] for s in sites)
    print("\n" + "=" * 78)
    print("TOTAL: %d rows returned, %d net new (%.1f%%)"
          % (total_rows, total_new, 100.0 * total_new / max(total_rows, 1)))
    print("Nothing was written. This script has no --apply.")
    print("=" * 78)


if __name__ == "__main__":
    main()
