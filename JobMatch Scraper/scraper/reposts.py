"""
scraper/reposts.py — the same role, posted again under a new URL.

A DIFFERENT question from deduplication, and worth keeping the two apart. `canonical_url` and
`fingerprint_duplicate` answer "are these two rows the same posting?" and their job is to stop the
second one being stored. This module answers "has this employer posted this role repeatedly over
months?", where every row is a genuinely distinct requisition and none of them should be removed.

Why it matters: a role that keeps coming back is information about the EMPLOYER, not a defect in
the feed. It usually means a req nobody fills — bad comp, a hiring manager who cannot decide, or a
pipeline being farmed for résumés. Kunal should see that before spending an evening on a cover
letter. It also gives `verify_dates` a second signal: if the same title+company appeared at three
URLs across ninety days, the "posted 2 days ago" on the newest one is technically true and
practically misleading.

Reimplemented from santifer/career-ops `detect-reposts.mjs` (MIT). The clustering is pure functions,
no network, no database — the caller supplies rows, which is what makes the thresholds testable.

REPORT ONLY by design. Nothing here closes, hides, deletes or re-scores a row. `--write` publishes
one KV row holding the cluster map, and that is the only write in the file.

RUNNABLE AS `python -m scraper.reposts`, and that placement is deliberate rather than tidy. Only
`scraper/`, `resume_brain/`, `templates/`, `static/` and a named list of root modules reach the
cPanel box (see ../.cpanel.yml); `scripts/` does NOT, so a cron line pointing there could never
work. It also matches how bin/cron_scrape.sh already invokes `-m scraper` and
`-m scraper.score_jobs`.

web.py imports this module (lazily, for cluster_key) which therefore drags in the whole `scraper`
package, since `scraper/__init__.py` imports `db` and builds SOURCES at module scope. MEASURED
before worrying about it: 31 ms and six new top-level modules on first use, against the 377 ms
web.py already spends importing `core` — so it is not worth splitting the key helpers out into a
root module to avoid. `core` and `db` are still imported inside main() rather than at the top,
which keeps the CLI's own dependencies off the fast path even though the package init has already
paid for db.

    python -m scraper.reposts                     # top 25 clusters
    python -m scraper.reposts --top 60 --min-urls 5
    python -m scraper.reposts --window 45 --company oracle
    python -m scraper.reposts --write             # publish the map the feed badges from
"""

import re

# The KV row the feed reads. Same pattern as close_dead_jds' jd_host_verdicts: a precomputed
# measurement in a KV blob, not a new column — DDL cannot go through the HMAC proxy, so a schema
# change would mean someone pasting SQL into a console, and this needs neither.
REPOST_KEY = "repost_clusters"

# ---------------------------------------------------------------------------------------------
# WHY THIS IS NOT career-ops' JACCARD >= 0.6 RULE.
#
# It was, and measured against the live corpus it flagged 3,503 clusters covering 13,636 of 25,180
# postings. A signal that fires on 54% of the feed is not a signal. Reading the top 20 showed why:
#
#   * Walmart "(USA) Distinguished, Software Engineer" clustered with Principal / Senior /
#     Software Engineer II. Those are four different jobs. Jaccard on {usa,distinguished,software,
#     engineer} vs {usa,principal,software,engineer} is 3/5 = 0.60 — it passed on the floor.
#   * "Architectural Project Manager" clustered with "Project Manager" at 2/3 = 0.67. A discipline
#     qualifier makes it a DIFFERENT role, not a repost of a broader one.
#   * Capital One "Lead Software Engineer" swallowed "(Golang)", "(Python)" and "- AML Reporting".
#
# So the rule is now EQUALITY OF THE CORE, not overlap: strip the baseline words and the two titles
# must have the same remaining token set. "Senior Data Engineer II" and "Data Engineer" both reduce
# to {data, engineer} and match; every case above now differs by a real word and does not. This is
# strictly tighter than a ratio and it cannot be tuned into a false positive by title length.
# ---------------------------------------------------------------------------------------------

# Words that are genuinely noise in a job title: grammar, work arrangement, country tags,
# employment type. Stripping these lets "Engineer, Data Platform (Remote, USA)" match
# "Data Platform Engineer".
#
# SENIORITY IS DELIBERATELY NOT HERE, and that is the second thing the live corpus corrected.
# With senior / sr / lead / principal / associate / II / III in this set, Amazon's "Operations
# Manager" clustered with "Senior Operations Manager", Walmart's "Principal, Software Engineer"
# swallowed Senior / II / III, and Actalent's "Associate Test Engineer" absorbed Lead and Senior.
# Those are different requisitions at different levels, not one posting reappearing. A genuine
# repost almost always keeps its exact title string, which the exact-match path already catches;
# the token path only exists to absorb word order and punctuation.
BASELINE_TOKENS = frozenset((
    "the", "and", "of", "for", "a", "an", "to", "in", "at", "with", "on",
    "remote", "hybrid", "onsite", "on-site", "virtual", "telecommute",
    "us", "usa", "u", "s", "united", "states",
    "contract", "contractor", "fulltime", "parttime", "temp", "temporary", "permanent",
    "f", "t", "p",                       # what "F/T" and "P/T" tokenise to
))

# How far apart two sightings can be and still count as one repost cluster. Beyond this it is not a
# repost, it is a role the company hires for periodically — which is normal and not worth flagging.
DEFAULT_WINDOW_DAYS = 90

# A cluster needs this many distinct URLs before it means anything. Two is the minimum that can
# possibly be a repost.
MIN_DISTINCT_URLS = 2

_WORD_RE = re.compile(r"[a-z0-9]+")

# Suffixes companies append to their own name inconsistently across boards, so "Acme Inc." and
# "Acme, LLC" cluster together rather than looking like two employers.
_CORP_SUFFIX_RE = re.compile(
    r"\b(inc|inc\.|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|"
    r"plc|gmbh|ag|sa|nv|bv|pty|group|holdings|holding|technologies|technology)\b")


def normalize_company(name):
    """Employer identity, tolerant of how differently boards spell the same company."""
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = _CORP_SUFFIX_RE.sub(" ", s)
    return " ".join(s.split())


def normalize_location(loc):
    """Location identity, loose enough to survive how differently boards write one place.

    Only the first two comma-separated parts are kept ("Brown Deer, WI, United States" ->
    "brown deer wi"), because the country tag is constant across a US-only feed and a third part
    is usually "United States" or a region label that some boards omit.
    """
    parts = [p for p in re.split(r"\s*,\s*", (loc or "").strip()) if p]
    s = " ".join(parts[:2]).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def title_tokens(title):
    """Deduped, order-free tokens. Order-free on purpose: "Engineer, Data Platform" and
    "Data Platform Engineer" are the same job advertised by two different recruiters."""
    return frozenset(_WORD_RE.findall((title or "").lower()))


def core_tokens(title):
    """The tokens that carry the role's identity — everything except seniority, articles and
    work-arrangement noise. This is what two titles have to agree on exactly."""
    return title_tokens(title) - BASELINE_TOKENS


def titles_match(a, b):
    """Same role? Exact string first because it is the overwhelming majority and costs nothing,
    then equality of the core."""
    if not a or not b:
        return False
    if a.strip().lower() == b.strip().lower():
        return True
    ca, cb = core_tokens(a), core_tokens(b)
    # An empty core means the title was nothing but seniority words ("Senior II"), which cannot
    # identify a role — refuse rather than match everything else that also reduces to nothing.
    return bool(ca) and ca == cb


def _day(s):
    """'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' -> the date part, or '' — never raises."""
    return (str(s or "")[:10]) if re.match(r"^\d{4}-\d{2}-\d{2}", str(s or "")) else ""


def _span_days(days):
    """Calendar span of a set of ISO dates, in days. The strings are already shape-validated by
    _day, so this only has to subtract two dates."""
    import datetime
    ds = sorted({d for d in days if d})
    if len(ds) < 2:
        return 0
    lo = datetime.date(*[int(x) for x in ds[0].split("-")])
    hi = datetime.date(*[int(x) for x in ds[-1].split("-")])
    return (hi - lo).days


def _collapse_by_url(rows):
    """One row per URL, keeping the EARLIEST sighting.

    A URL seen on ten scrape days is one posting seen ten times, and counting those as ten would
    make every long-lived posting look like a serial repost — the failure mode this guard exists
    for.
    """
    best = {}
    for r in (rows or []):
        url = (r.get("url") or "").strip()
        if not url:
            continue
        seen = _day(r.get("first_seen") or r.get("found_date"))
        cur = best.get(url)
        if cur is None or (seen and (not cur["_day"] or seen < cur["_day"])):
            best[url] = dict(r, _day=seen)
    return list(best.values())


def cluster_key(title, company, location):
    """The identity a repost cluster is stored under: company + location + sorted core tokens.

    ONE definition, shared by main() below (which writes the map) and web.py (which
    looks a feed row up in it). Keying the stored map on this rather than on URLs matters for two
    reasons: it is ~500 entries instead of ~2,200, and a NEWLY scraped posting of an
    already-reposted role is badged the moment it lands, without anyone re-running the script.
    """
    core = " ".join(sorted(core_tokens(title)))
    if not core:
        return ""
    return "%s|%s|%s" % (normalize_company(company), normalize_location(location), core)


def cluster_map(clusters):
    """{cluster_key: distinct-URL count} — the shape stored in the KV and read by the feed.

    Built from the keys the grouping ALREADY used, not by re-normalising the display fields. Those
    two must produce identical strings or no badge ever appears, and it would fail silently — an
    empty lookup is indistinguishable from "nothing is reposted". test_reposts pins the invariant.
    """
    out = {}
    for c in clusters or []:
        core = " ".join(sorted(core_tokens(c["title"])))
        if not core:
            continue
        k = "%s|%s|%s" % (c["company_key"], c["location_key"], core)
        out[k] = max(out.get(k, 0), c["count"])
    return out


def find_reposts(rows, window_days=DEFAULT_WINDOW_DAYS, min_urls=MIN_DISTINCT_URLS):
    """[{company, title, urls, dates, count, span_days}] — one entry per repost cluster.

    `rows` are dicts with title / company / url and first_seen (or found_date). Sorted by count
    descending, because a role posted five times is a louder signal than one posted twice.
    """
    by_group = {}
    for r in _collapse_by_url(rows):
        key = normalize_company(r.get("company"))
        if key and (r.get("title") or "").strip():
            # LOCATION IS PART OF THE KEY, and leaving it out was the single biggest source of
            # false positives. core.posting_key already documents why: "Amazon genuinely lists 431
            # Operations Manager roles and Walmart 144 store-level pharmacy internships. Those are
            # inventory, not duplicates." Without location, Walmart's Pharmacy Pre-Grad Intern
            # read as 106 reposts when it is one role advertised at 106 stores.
            by_group.setdefault((key, normalize_location(r.get("location"))), []).append(r)

    out = []
    for (company, location), items in sorted(by_group.items()):
        # Cheap bucketing first: exact case-insensitive title. Most reposts are a literal re-post
        # of the same string, so this resolves the bulk in O(n) and leaves few buckets to compare.
        buckets = {}
        for r in items:
            buckets.setdefault(r["title"].strip().lower(), []).append(r)

        # Then merge distinct buckets that are fuzzily the same role. Greedy single pass: each
        # bucket joins the first cluster it matches, so this is O(buckets * clusters) rather than
        # O(n^2) over every posting.
        clusters = []
        for _key, group in sorted(buckets.items()):
            for c in clusters:
                if titles_match(c["title"], group[0]["title"]):
                    c["rows"].extend(group)
                    break
            else:
                clusters.append({"title": group[0]["title"], "rows": list(group)})

        for c in clusters:
            urls = sorted({r["url"] for r in c["rows"]})
            if len(urls) < min_urls:
                continue
            dates = sorted({r["_day"] for r in c["rows"] if r["_day"]})
            span = _span_days(dates)
            # Every sighting has to sit inside one window. A span WIDER than the window is a role
            # the company hires for periodically, which is normal.
            if dates and span > window_days:
                continue
            out.append({
                "company": c["rows"][0].get("company") or company,
                "company_key": company,
                "location": c["rows"][0].get("location") or "",
                "location_key": location,
                "title": c["title"],
                "urls": urls,
                "dates": dates,
                "count": len(urls),
                "span_days": span,
                "titles": sorted({r["title"] for r in c["rows"]}),
            })
    out.sort(key=lambda x: (-x["count"], -x["span_days"], x["company_key"], x["title"]))

    return out


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------
import argparse            # noqa: E402  (CLI-only imports, kept off the web.py import path)
import collections         # noqa: E402
import datetime            # noqa: E402
import json                # noqa: E402


def _today():
    return datetime.date.today().isoformat()


def _rows():
    """Every stored posting, with only the columns the clustering reads.

    include_jd=False matters: descriptions are the bulk of the table and pulling them here would
    turn a cheap report into the kind of read that put us over the free-tier egress budget once
    already.
    """
    # `location` is NOT optional here, and leaving it out silently disabled the location half of
    # the cluster key: every row normalised to "" and Walmart's 106 store-level internships still
    # read as 106  The clustering has no way to tell an absent column from a blank field.
    import db
    return db.load_jobs(include_jd=False,
                        cols="url,title,company,location,first_seen,found_date,is_active") or []


def main():
    import core
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25, help="clusters to print (default 25)")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS,
                    help="days all sightings must fall within (default %d)"
                         % DEFAULT_WINDOW_DAYS)
    # 3, not the module's theoretical minimum of 2. Measured against the live 25,180-row corpus:
    #   min 2 -> 1,845 clusters / 4,815 postings (19% of the feed)
    #   min 3 ->   528 clusters / 2,181 postings (8.7%)
    #   min 5 ->   122 clusters /   863 postings (3.4%)
    # Two sightings is a coincidence often enough that 19% of the feed gets flagged, and a signal
    # that fires on a fifth of the corpus is not one anybody can act on. Three is where the list
    # becomes a list worth reading.
    ap.add_argument("--min-urls", type=int, default=3,
                    help="distinct URLs before a cluster counts (default 3)")
    ap.add_argument("--company", default="", help="substring filter on the employer name")
    ap.add_argument("--include-closed", action="store_true",
                    help="also cluster rows already marked is_active=false")
    ap.add_argument("--json", default="", help="write the full result to this file")
    ap.add_argument("--write", action="store_true",
                    help="publish the cluster map to the %s KV row so the feed can badge cards"
                         % REPOST_KEY)
    args = ap.parse_args()

    rows = _rows()
    print("read %d row(s) from the corpus" % len(rows))
    if not args.include_closed:
        # A closed row is a posting that ENDED, which is exactly what a repost cycle looks like
        # from the outside, so including them by default would inflate every count. Off by
        # default, available when the question is "how long has this been going on".
        before = len(rows)
        rows = [r for r in rows if r.get("is_active") is not False]
        print("  (dropped %d closed row(s); --include-closed to keep them)" % (before - len(rows)))
    if args.company:
        want = args.company.lower()
        rows = [r for r in rows if want in (r.get("company") or "").lower()]
        print("  (filtered to %d row(s) matching company=%r)" % (len(rows), args.company))

    clusters = find_reposts(rows, window_days=args.window, min_urls=args.min_urls)
    total_rows = sum(c["count"] for c in clusters)
    print("\n%d repost cluster(s), covering %d posting(s) — window %dd, min %d URL(s)\n"
          % (len(clusters), total_rows, args.window, args.min_urls))

    for c in clusters[:args.top]:
        span = "same day" if c["span_days"] == 0 else "%dd apart" % c["span_days"]
        print("%2dx  %-34s %-52s %s" % (c["count"], (c["company"] or "?")[:34],
                                        c["title"][:52], span))
        if len(c["titles"]) > 1:
            print("        also as: %s" % "; ".join(t[:60] for t in c["titles"][1:4]))
        if c["dates"]:
            print("        seen: %s" % ", ".join(c["dates"][:6])
                  + (" …" if len(c["dates"]) > 6 else ""))
        print("        %s" % c["urls"][0][:110])

    if clusters[args.top:]:
        print("\n… %d more (use --top)" % len(clusters[args.top:]))

    # Which employers do it most. This is the number worth acting on: one repeatedly-reposted role
    # is a coincidence, an employer with fifteen of them is a pattern.
    worst = collections.Counter()
    for c in clusters:
        worst[c["company"] or "?"] += 1
    if worst:
        print("\nemployers with the most repost clusters:")
        for name, n in worst.most_common(12):
            print("  %-40s %d" % (name[:40], n))
        # Staffing agencies dominate this list by construction — Actalent alone held 167 of the 528
        # clusters at min-urls=3 — because re-advertising the same role for different clients IS
        # their product. Worth knowing, but not the same finding as a direct employer sitting on a
        # req nobody fills.
        print("  (staffing agencies re-advertise by design — read them differently from a direct"
              " employer)")

    # Aggregator relists are a DIFFERENT problem (fingerprint_duplicate's), so say when a cluster
    # is one, rather than quietly counting it as employer behaviour.
    agg = [c for c in clusters if any(core.is_aggregator_url(u) for u in c["urls"])]
    if agg:
        print("\nnote: %d cluster(s) include an aggregator URL — those may be relists rather than"
              "\n      the employer re-posting. See scraper.fingerprint_duplicate." % len(agg))

    if args.write:
        # Only clusters at or above the threshold are published, so the badge and this report
        # always agree about what counts as a repost.
        cmap = cluster_map(clusters)
        import db
        db.put_kv(REPOST_KEY, {"clusters": cmap, "built": _today(),
                               "window_days": args.window, "min_urls": args.min_urls,
                               "rows_scanned": len(rows)})
        print("\npublished %d cluster key(s) to the %s KV row — the feed badges from this."
              % (len(cmap), REPOST_KEY))
        print("workers cache it for their lifetime, so a running app picks it up on next restart.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(clusters, fh, indent=1, ensure_ascii=False)
        print("\nwrote %s (%d cluster(s))" % (args.json, len(clusters)))


if __name__ == "__main__":
    main()
