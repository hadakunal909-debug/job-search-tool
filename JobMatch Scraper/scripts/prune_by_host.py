#!/usr/bin/env python3
"""Delete job rows served from a given HOST. The rollback for turning an aggregator source on.

Written before the JobSpy source was enabled and intended never to run. Its job is to make the
change reversible: unsetting JOBSPY_SITES stops new rows arriving, but it does nothing about the
ones already stored, and "we can always undo it" is only true if the undo exists.

    python scripts/prune_by_host.py --host indeed.com                    # dry run
    python scripts/prune_by_host.py --host indeed.com --apply
    python scripts/prune_by_host.py --host indeed.com --host glassdoor.com --apply
    python scripts/prune_by_host.py --aggregators                        # every aggregator host

Matching is on a host SUFFIX, so --host indeed.com covers www.indeed.com and ca.indeed.com but
never notindeed.com. It is checked against the parsed host, not with a substring search over the
URL — a naive `"indeed.com" in url` would also delete an employer's own posting that happened to
carry ?utm_source=indeed.com.

Rows you have liked, applied to or hidden are protected unless --include-flagged, the same
contract as prune_offtarget.py and the admin delete.
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db


def _host_matches(url, wanted):
    """True when the URL's host IS one of `wanted`, or a subdomain of one."""
    host = core.url_host(url)
    if not host:
        return False
    host = host.split(":", 1)[0]                      # drop any port
    return any(host == w or host.endswith("." + w) for w in wanted)


def _make_predicate(wanted, aggregators):
    """What counts as a row to remove.

    --aggregators delegates to core.is_aggregator_url rather than reusing its host list here:
    core.AGGREGATOR_HOSTS holds SUBSTRING patterns ("adzuna."), not domains, so treating them as
    domains silently matches nothing. Deferring to the one function that knows how to read them
    keeps "aggregator" meaning the same thing here, in the feed, and in the autoapply queue.
    """
    if aggregators and wanted:
        return lambda u: core.is_aggregator_url(u) or _host_matches(u, wanted)
    if aggregators:
        return core.is_aggregator_url
    return lambda u: _host_matches(u, wanted)


def _score(row):
    s = str(row.get("match_score") or "")
    return int(s) if s.isdigit() else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", action="append", default=[],
                    help="host to remove (repeatable), e.g. indeed.com")
    ap.add_argument("--aggregators", action="store_true",
                    help="shorthand for every host in core.AGGREGATOR_HOSTS")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--include-flagged", action="store_true",
                    help="also delete jobs you've liked / applied to / hidden")
    a = ap.parse_args()

    wanted = [h.strip().lower().lstrip(".") for h in a.host if h.strip()]
    if not (wanted or a.aggregators):
        ap.error("give at least one --host, or --aggregators")
    matches = _make_predicate(wanted, a.aggregators)

    print("removing rows served from: %s"
          % ", ".join(wanted + (["every aggregator host"] if a.aggregators else [])))
    rows = db.load_jobs(include_jd=False, cols=db.COLS_DEDUPE)
    total = len(rows)
    doomed_rows = [r for r in rows if r.get("url") and matches(r["url"])]

    flagged = db.all_flagged_urls()
    protected = [r for r in doomed_rows if r["url"] in flagged]
    keep_flagged = not a.include_flagged
    doomed = [r for r in doomed_rows if not (keep_flagged and r["url"] in flagged)]
    urls = [r["url"] for r in doomed]

    print("\nrows in table        : %d" % total)
    print("on these hosts       : %d (%.1f%%)"
          % (len(doomed_rows), 100.0 * len(doomed_rows) / max(total, 1)))
    print("  saved/applied/hidden among them: %d  (%s)"
          % (len(protected), "WILL BE DELETED" if a.include_flagged else "protected"))
    print("to delete            : %d" % len(urls))
    print("would remain         : %d" % (total - len(urls)))

    if doomed:
        print("\nby host:")
        for host, n in collections.Counter(
                core.url_host(r["url"]) for r in doomed).most_common(12):
            print("   %-38s %6d" % (host[:38], n))
        print("\nbiggest contributors:")
        for c, n in collections.Counter(
                (r.get("company") or "?") for r in doomed).most_common(10):
            print("   %-38s %6d" % (c[:38], n))
        print("\nsample:")
        for r in doomed[:6]:
            print("   %-30s %-40s %s" % ((r.get("company") or "?")[:30],
                                         (r.get("title") or "")[:40], r["url"][:60]))

    if not a.apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply.")
        return
    if not urls:
        print("\nnothing to do.")
        return

    def prog(done, tot):
        if done % 500 == 0 or done == tot:
            print("   deleted %d/%d" % (done, tot), flush=True)

    print("\ndeleting...")
    # remote_only when Supabase is the real backend: with it briefly unreachable the CSV
    # fallback would rewrite an absent jobs.csv, report "0 removed", and leave the rows there.
    n = db.delete_urls(urls, progress=prog, remote_only=db.has_remote_db())
    left = db.table_count(db.TABLE)
    print("\ndeleted %d row(s). table now holds %s (was %d)." % (n, left, total))


if __name__ == "__main__":
    main()
