#!/usr/bin/env python3
"""build_companies_table.py — resolve every employer's facts once, into public.companies.

    python scripts/build_companies_table.py              # dry run: counts + a sample
    python scripts/build_companies_table.py --apply      # write the table and bump the version
    python scripts/build_companies_table.py --check      # exit 1 if the table is stale/missing

WHAT THIS REPLACES. web._build_row asks eleven questions about the EMPLOYER on every card of
every render -- logo, aspect, mono, initials, sponsor strength, sponsor count, visa routes,
visa_likely, E-Verify, agency, cap-exempt -- and answers them from files loaded into each
Passenger worker. Measured on the live corpus 2026-09-06:

    sponsor_counts.json   129,660 keys   3.3 MB on disk ->  11.8 MB resident, per worker
    visa_tags.json        123,472 keys   2.9 MB on disk ->  11.2 MB resident, per worker

...to answer questions about 3,395 employers. 98.5% of those rows are never asked about; they are
the federal filing universe, not our corpus. The answers do not change between scrapes, so they
are computed here, once, and stored.

THE UNIVERSE IS THE CORPUS PLUS companies.json, not the filing files. That is the whole saving:
we resolve the 3,395 employers we actually have jobs from rather than shipping 129,660 rows to
find them. The cost of that choice is that an employer we scrape for the first time has no row
until this runs again -- measured at ~162 new employers a day -- and a missing row degrades to
"no sponsorship data", which is already what an employer absent from the federal files gets. Run
this after a scrape, or after an adoption run, for the same reason companies.json needs it.

IT REUSES THE FEED'S OWN ACCESSORS rather than reimplementing them. web.logo_url and friends do a
two-step slug lookup through an alias map; core.sponsor_strength has a two-tier key fallback;
core.is_everify has a rapidfuzz fallback. A second copy of any of those would drift, and a
build-time answer that disagrees with the request-time one is worse than no table at all.
"""
import argparse
import collections
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EV_OFF", "1")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db

# web imports at module scope and needs APP_SECRET when a remote database is configured (it
# refuses to sign cookies with a guessable dev key). Set a build-only value BEFORE the import if
# the caller has not: this process never serves a request and never signs anything a browser
# sees.
os.environ.setdefault("APP_SECRET", "build-companies-table-not-a-session-key")
import web                                                            # noqa: E402


def _bit_index():
    """{normalised name: bitmask} straight from visa_tags.json.

    core.visa_tags() expands the mask into tag KEYS; the table stores the int so core.VISA_TAGS
    stays the single definition of what each bit means and a future tag needs no migration.
    """
    return core.load_visa_tags() or {}


def build(rows_source=None):
    """[{companies-row}] for every employer worth holding facts about."""
    counts = core.load_sponsor_counts() or {}
    years = core.load_sponsor_years() or {}
    bits = _bit_index()
    ev = getattr(web, "_EVERIFY_INDEX", None)

    # THE CORPUS FIRST, so display_name is the spelling a card actually shows, then anything in
    # companies.json the corpus has no jobs for (the /companies directory is wider than the feed).
    seen = collections.Counter()
    if rows_source is None:
        rows_source = db.load_jobs(cols="company,is_active")
    for r in rows_source:
        if r.get("is_active") in (None, True, "true", "t", 1):
            c = (r.get("company") or "").strip()
            if c:
                seen[c] += 1
    from_corpus = len(seen)
    try:
        for row in (web.companies_blob() or {}).get("rows") or []:
            c = (row[0] or "").strip() if row else ""
            if c and c not in seen:
                seen[c] = 0
    except Exception as e:
        print("  (companies.json not readable, corpus only: %s)" % str(e)[:80])

    # One row per NORMALISED key. Two spellings can collapse onto one key -- "Acme Inc" and
    # "Acme, Inc." -- and the display name should be the one the corpus uses most, not whichever
    # happened to sort first.
    # EVERY SPELLING, not just the winner. The display name is the one the corpus uses most,
    # which is the right thing to SHOW -- and the wrong thing to resolve a logo from. Measured
    # against the live manifest: keying the logo on the display name alone LOST the logo for
    # EchoStar and Lonza, because the majority spelling ('EchoStar Corporation') has no slug
    # while the minority one does. web._logo_slug tries a direct slug and then an alias map,
    # and which of the two hits depends on the exact string.
    #
    # So the row takes the logo from whichever spelling of this employer resolves one. That
    # also fills 11 gaps in the other direction -- 'Apple Inc', 'Accenture LLP', 'Micron
    # Technology' (20 postings) all rendered a monogram before, because the suffix defeated
    # the direct slug and the alias map had no entry.
    spellings = collections.defaultdict(list)
    best = {}
    for name, n in seen.most_common():
        key = core.norm_company(name)
        if not key:
            continue
        spellings[key].append(name)
        if key not in best:
            best[key] = name

    out = []
    for key, name in sorted(best.items()):
        # display name first, so a tie goes to what the reader sees
        logo = (None, None, False)
        for cand in [name] + [s for s in spellings[key] if s != name]:
            u = web.logo_url(cand)
            if u:
                logo = (u, web.logo_ar(cand) or None, bool(web.logo_mono(cand)))
                break
        strength, scount = core.sponsor_strength(name, counts)
        out.append({
            "name_key": key,
            "display_name": name,
            "logo_url": logo[0], "logo_ar": logo[1], "logo_mono": logo[2],
            "initials": web.initials(name) or None,
            "sector": None,             # /companies still renders companies.json -- see the SQL
            "is_agency": bool(core.is_agency(name)),
            "is_cap_exempt": bool(core.is_cap_exempt(name)),
            "is_everify": bool(core.is_everify(name, ev)) if ev else False,
            "h1b_count": int(scount or 0) or None,
            "h1b_by_fy": years.get(key) or None,
            "visa_bits": int(bits.get(key) or 0),
            "careers_url": None,
        })
    return out, from_corpus


def fingerprint(rows):
    """A stable hash of the whole table, for data_versions.

    SORTED AND CANONICAL, because this decides whether the row cache rebuilds. A dict whose key
    order moved would bump the version, invalidate every built row and charge the next visitor a
    full rebuild for a file that had not changed -- the same shape as the mtime-vs-content bug
    _derived_signature already had to fix once.
    """
    blob = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the stored table does not match a fresh build")
    ap.add_argument("--sample", type=int, default=6)
    a = ap.parse_args()

    rows, from_corpus = build()
    fp = fingerprint(rows)
    print("employers: %d  (%d from the live corpus, %d directory-only)"
          % (len(rows), from_corpus, len(rows) - from_corpus))
    have = lambda k: sum(1 for r in rows if r.get(k))                        # noqa: E731
    for k in ("logo_url", "initials", "h1b_count", "h1b_by_fy"):
        print("  with %-12s %5d  (%.0f%%)" % (k, have(k), 100.0 * have(k) / (len(rows) or 1)))
    for k in ("is_agency", "is_cap_exempt", "is_everify"):
        print("  %-17s %5d" % (k, sum(1 for r in rows if r.get(k))))
    print("  with a visa route  %5d" % sum(1 for r in rows if r.get("visa_bits")))
    print("  fingerprint: %s" % fp)

    for r in rows[:a.sample]:
        print("     %-30s logo=%-5s h1b=%-7s bits=%d" % (
            r["display_name"][:30], bool(r["logo_url"]), r["h1b_count"], r["visa_bits"]))

    if a.check:
        stored = db.get_data_version("companies")
        if stored == fp:
            print("\nup to date.")
            return 0
        print("\nSTALE: stored version %r, a fresh build is %r." % (stored or "(none)", fp))
        print("Run: python scripts/build_companies_table.py --apply")
        return 1

    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    n = db.save_companies(rows)
    # THE VERSION IS BUMPED AFTER THE ROWS LAND, never before. web._derived_signature keys the
    # row cache on it, so a version that moved ahead of its data would mark the cache fresh for
    # rows that are not there yet -- and the cache would then be trusted until the NEXT build.
    db.set_data_version("companies", fp)
    print("\nwrote %d employer row(s); data_versions['companies'] = %s" % (n, fp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
