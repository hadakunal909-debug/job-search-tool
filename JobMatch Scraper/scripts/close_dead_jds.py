#!/usr/bin/env python3
"""close_dead_jds.py — stop a row that can never have a description from pretending otherwise.

Every other JD tool here is about GETTING descriptions. This one is about the rows that provably
cannot have one, and it exists because leaving them alone is not neutral:

  * a row holding a 46-character loading shell scores 0 and reads blank, while its non-empty
    `jd` column keeps it out of every fetch queue. Clearing the shell is what makes it honest
    AND retryable.
  * a row whose posting returns 404 is a job nobody can apply to. It sits in the feed as
    "description pending" forever, and PRUNE_DAYS will not reach it for up to a month.
  * a row on a host that bot-walls every server-side request (Tesla behind Akamai, iCIMS behind
    an AWS WAF human-verification challenge) will NEVER gain a description this way. Telling the
    user "it'll get a match score once the full job description is fetched" — which is what the
    feed says today — is a promise that cannot be kept.

Three actions, each optional, none of them a delete:

  CLEAR    blank a junk description in BOTH stores, the `jd` column and jd_cache.json.gz.
           Never one without the other: the scorer reads the CACHE, so clearing only the column
           leaves it tokenising the shell (this is the 621-row half-repair of 2026-08-17).
  CLOSE    is_active=false on rows whose posting is provably gone (404/410, or a Workday CXS
           "not found"). The feed already hides closed rows; nothing is deleted, Saved/Applied
           history survives, and PRUNE_DAYS still owns retirement.
  RECORD   write a per-host verdict to the jd_host_verdicts KV row, which is what lets the feed
           say "this employer does not publish a readable description" instead of "pending".

    python scripts/close_dead_jds.py                      # dry run, probes and reports
    python scripts/close_dead_jds.py --per-host 3
    python scripts/close_dead_jds.py --apply              # clear + close + record
    python scripts/close_dead_jds.py --apply --no-close    # clear and record only
"""
import argparse
import collections
import concurrent.futures
import datetime
import os
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db
import scraper
import scraper.liveness as liveness
import scraper.score_jobs as sj

VERDICT_KEY = "jd_host_verdicts"
# A host needs this many probed rows agreeing before its verdict is recorded. One 404 is a
# closed posting; three 404s out of three is a statement about the host.
MIN_AGREE = 2

# url -> why, per non-gone verdict. Populated by _probe purely so a survey run can say WHY it
# declined to close something, which is the question anyone reading this output actually has.
_WHY = {}


def _probe(url, title="", location=""):
    """(verdict, status, bytes, jd) for one stored URL. Network; writes nothing itself.

    Returns the TEXT, not just its length, because a probe that finds a description and then
    throws it away is the same waste this whole file is about. Whatever it finds is written by
    the RECOVER step in main().
    """
    status, nbytes, body, final_url = "", 0, "", ""
    try:
        r = scraper._safe_get(url, timeout=20)
        # The BODY, not just its length. Every guard in scraper.liveness that distinguishes a bot
        # wall from a withdrawn posting needs the text, and _safe_get already paid for it.
        body = r.text or ""
        status, nbytes, final_url = str(r.status_code), len(body), (r.url or "")
    except Exception as e:
        status = "ERR:" + type(e).__name__
    jd = ""
    try:
        jd = sj.detail_jd(url)[1] or ""
    except Exception:
        pass
    if len(jd) < core._MIN_JD_CHARS:
        # LAST RESORT, and only for a host where a withdrawn id does not mean a withdrawn ROLE.
        # amazon.jobs 404s on 27 stored ids while search.json still answers 200 with the full
        # text, because Amazon reposts the same role under a new id constantly. The match is
        # exact-title + city + exactly-one-candidate; see amazon_rematch_jd for why anything
        # looser is worse than a blank.
        try:
            jd = sj.amazon_rematch_jd(url, title, location) or jd
        except Exception:
            pass
    if len(jd) >= core._MIN_JD_CHARS:
        return "readable", status, nbytes, jd
    # Everything else is scraper.liveness's call. It was inline here and had no idea about bot
    # walls, 429/503, or a redirect that lands on a listing page -- see that module's docstring
    # for what each hole cost. Only "gone" ever closes a row (see CLOSES_THE_POSTING).
    verdict, why = liveness.classify(status, body, url, final_url)
    if verdict != "gone":
        _WHY.setdefault(verdict, {})[url] = why
    return verdict, status, nbytes, jd


def main():
    ap = argparse.ArgumentParser()
    # 0 = probe EVERY row with no usable description. That is the default because the two
    # destructive-ish actions are decided per ROW: closing a posting because three of its
    # host-mates 404'd is an extrapolation, not a measurement, and this script's whole claim is
    # that it only acts on what it verified. Cap it for a quick survey.
    ap.add_argument("--per-host", type=int, default=0,
                    help="rows to probe per host (0 = all of them; per-row decisions need this)")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--host", action="append", default=[], help="only this host (repeatable)")
    ap.add_argument("--no-close", action="store_true", help="skip is_active=false")
    ap.add_argument("--no-clear", action="store_true", help="skip blanking junk descriptions")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    # title and location are here ONLY for the Amazon re-match in _probe; every other check
    # works off the URL alone.
    rows = db.load_jobs(cols="url,company,title,location")
    meta = {r["url"]: (r.get("title") or "", r.get("location") or "")
            for r in rows if r.get("url")}
    all_urls = {r["url"] for r in rows if r.get("url")}
    empty = db.urls_missing_jd() & all_urls
    cache = sj._load_jd_cache()
    thin = {u for u in all_urls if u not in empty and sj._is_thin_jd(cache.get(u))}

    bad = sorted(empty | thin)
    if a.host:
        bad = [u for u in bad if urlsplit(u).netloc in set(a.host)]
    by_host = collections.defaultdict(list)
    for u in bad:
        by_host[urlsplit(u).netloc].append(u)

    print("corpus %d rows | %d with no usable description (%d empty, %d holding a shell)"
          % (len(all_urls), len(bad), len(empty & set(bad)), len(thin & set(bad))))
    print("%d host(s) to probe, %d row(s) sampled\n"
          % (len(by_host), sum(min(len(v), a.per_host) for v in by_host.values())))

    sample = [(h, u) for h, us in by_host.items()
              for u in (sorted(us)[:a.per_host] if a.per_host else sorted(us))]
    results = collections.defaultdict(list)
    recovered = {}

    def _one(hu):
        u = hu[1]
        t, loc = meta.get(u, ("", ""))
        return _probe(u, t, loc)

    per_row = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, ((h, u), out) in enumerate(zip(sample, ex.map(_one, sample)), 1):
            results[h].append(out)
            per_row[u] = out[0]
            if out[0] == "readable":
                recovered[u] = out[3]
            if i % 200 == 0:
                print("   ... probed %d/%d" % (i, len(sample)), flush=True)

    verdicts, today = {}, datetime.date.today().isoformat()
    print("%-42s %6s  %-10s %-18s %s" % ("HOST", "ROWS", "VERDICT", "statuses", "note"))
    for h, us in sorted(by_host.items(), key=lambda kv: -len(kv[1])):
        outs = results.get(h) or []
        if not outs:
            continue
        counts = collections.Counter(o[0] for o in outs)
        top, n = counts.most_common(1)[0]
        statuses = ",".join(sorted({o[1] for o in outs}))
        # A single readable row proves the host is fine, whatever the others said.
        if counts.get("readable"):
            top, note = "readable", "an extractor works here — do not close"
        elif n < MIN_AGREE and len(outs) >= MIN_AGREE:
            # "mixed", NOT "unknown". This branch and liveness.classify's "unknown" used to share
            # a label while meaning opposite things: this one is "the probes could not agree", and
            # that one is "every probe reached a real page and got no text out of it". The second
            # is a finding the feed can act on -- it is exactly the state Actalent's apply domain
            # is in -- and the first is an admission that we do not know. Merging them meant the
            # badge could not use either.
            top, note = "mixed", "probes disagreed"
        else:
            note = liveness.VERDICT_NOTES.get(top, "")
        print("%-42s %6d  %-10s %-18s %s" % (h[:42], len(us), top, statuses, note))
        verdicts[h] = {"verdict": top, "statuses": statuses, "rows": len(us),
                       "checked": today, "probed": len(outs)}

    # CLEAR: any thin row this run could not read. The stored value is under 400 characters and
    # the row scores 0, so it is provably not a description — whatever the reason the probe
    # failed, an empty column is the honest state and it puts the row back in the fetch queue.
    # This is the only action that applies to the "unknown" verdict (a 200 with real content and
    # still no text), which is where Actalent's 840 Salesforce shells live.
    clear = [u for u, v in per_row.items() if u in thin and v != "readable"]
    # CLOSE: only rows THIS RUN saw 404/410 (or a 200 with an empty body) for themselves. Never
    # inferred from a host-mate — is_active=false takes a job out of the feed, so it has to be
    # earned per row.
    close = [u for u, v in per_row.items() if v in liveness.CLOSES_THE_POSTING]

    if recovered:
        print("\nWOULD RECOVER %d description(s) the probe read successfully (avg %d chars)"
              % (len(recovered), sum(len(v) for v in recovered.values()) // len(recovered)))
    print("\nWOULD CLEAR %d junk description(s) (column + jd_cache), on %d host(s)"
          % (len(clear), len({urlsplit(u).netloc for u in clear})))
    print("WOULD CLOSE %d row(s) as gone (is_active=false; nothing deleted) — each one probed"
          % len(close))
    print("WOULD RECORD %d host verdict(s) so the feed can stop saying 'pending'" % len(verdicts))
    blocked_rows = sum(v["rows"] for v in verdicts.values() if v["verdict"] == "blocked")
    print("  of which %d row(s) are on hosts that refuse server-side reads — these stay in the"
          "\n  feed as live jobs, they just stop claiming a description is coming." % blocked_rows)

    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    if recovered:
        # Both stores, always. The scorer reads the cache; the site reads the column.
        db.update_jds(recovered)
        bank = sj._load_jd_cache()
        bank.update(recovered)
        sj._save_jd_cache(bank)
        print("\nwrote %d description(s) the probe recovered, to the database and the cache"
              % len(recovered))

    if clear and not a.no_clear:
        # BOTH stores, in this order. The column first (that is what the site renders), the
        # cache second (that is what the scorer analyses). Writing "" rather than deleting the
        # row keeps every other column, including Saved/Applied state, untouched.
        db.update_jds({u: "" for u in clear})
        bank = sj._load_jd_cache()
        for u in clear:
            bank.pop(u, None)
        sj._save_jd_cache(bank)
        print("\ncleared %d junk description(s) from the database and the disk cache" % len(clear))

    if close and not a.no_close:
        db.update_job_fields([{"url": u, "is_active": False} for u in close])
        print("closed %d row(s) (is_active=false)" % len(close))

    db.put_kv(VERDICT_KEY, {"hosts": verdicts})
    print("recorded %d host verdict(s) under %s" % (len(verdicts), VERDICT_KEY))
    try:
        db.audit_log("close_dead_jds", "jd_cleanup", count=len(clear) + len(close),
                     detail={"cleared": len(clear), "closed": len(close),
                             "hosts": len(verdicts)})
    except Exception:
        pass
    print("\nNow run: python -m scraper.score_jobs   (the cleared rows re-enter the fetch queue)")
    # AND THE BADGE WILL NOT APPEAR UNTIL THE ROW CACHE TURNS OVER. Measured 2026-09-02: the
    # verdicts above were written, web._host_jd_blocked resolved the host correctly,
    # _build_row returned jd_unavailable=True -- and /job STILL rendered "it gets a match
    # score after the next scoring run", because the page is served from the persisted base
    # rows in row_cache/, which were built before this ran and hold the old flag.
    #
    # That is by design and must not be "fixed" by putting the verdicts in the cache key:
    # web._derived_signature() excludes them deliberately, because a KV read whose failure
    # mode is {} made two workers compute different keys and overwrite each other's file
    # (production showed 63 ms and 8,401 ms in the same second). test_speed_caches.py pins
    # the exclusion with an explicit "jd_host_verdicts does NOT move the signature" check.
    #
    # So the invalidation is manual, and it is the same move a data-only DB fix needs:
    # jobs_fingerprint() is (row count, max first_seen, scored count) and recording a verdict
    # moves none of the three -- it is a KV write, and it does not touch the jobs table at all --
    # so nothing invalidates the built rows on its own.
    print("Then, so the feed shows it:  rm row_cache/*.rows.gz && touch tmp/restart.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
