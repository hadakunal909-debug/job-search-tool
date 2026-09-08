#!/usr/bin/env python3
"""pm_funnel.py -- how many of the jobs this reader wants actually reach the screen.

WHY THIS EXISTS. Every claim about "the feed hides jobs" in this repo used to be argued rather
than measured, and the arguments were wrong in both directions. This script is the measurement:
corpus -> role chips -> level -> each default preference -> each optional filter, with the number
each step costs. Run it before and after any change to a filter, a vocabulary, or a default.

THE LEVEL IS READ FROM THE DESCRIPTION, NOT THE TITLE, and that is the whole methodology.
Measured 2026-09-08 on 2,575 stored product descriptions: a title-based entry-level count says
208 where the descriptions say 176, over-claims 60 rows whose text asks for 3-5 years, and misses
95 whose titles do not say "associate" at all ("Product Manager II", "Product Owner I", four
Capital One "Senior Associate, Product Manager" reqs -- at Capital One that grade IS the
early-career rung). A title regex is wrong by ~29% in one direction and ~50% in the other, so
any funnel counted off titles is a different set, not just a different size.

    # read-only, and it needs the descriptions, so it is not cheap -- ~16 MB for the
    # product family. Never db.load_jobs() with no cols: that is the ~130 MB hazard
    # db._warn_full_jd_read exists to catch.
    DB_REQUIRE=proxy DB_PROXY_SECRET="$(tr -d '\r\n' < .db_proxy_secret)" \
      DB_PROXY_URL="https://stemjobs1.astrochakra.co/api/db" \
      EV_OFF=1 python scripts/pm_funnel.py --roles pm,program,product

EV_OFF=1 IS NOT OPTIONAL -- analytics.py reads it once at import, and an unguarded run here
would record feed_view events for a script (CLAUDE.md; one past feed_parity.py run wrote 98.8%
of all recorded events).

--cache writes the descriptions to a local file so the sweep can be re-run without re-reading
them. It is written with an explicit utf-8 encoding: a bare open() dies on the zero-width space
a real posting contains, which cost two runs while this was being written.
"""
import argparse
import collections
import datetime
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("EV_OFF", "1")

import core                                                          # noqa: E402
import db                                                            # noqa: E402

# The columns the funnel needs. Narrow on purpose -- see the docstring.
COLS = ("url,title,company,location,found_date,first_seen,is_active,match_score,"
        "exp_max_years,salary_min,salary_period,remote,loc_state")


def _age(row, today):
    s = (row.get("found_date") or row.get("first_seen") or "")[:10]
    try:
        return (today - datetime.date.fromisoformat(s)).days
    except Exception:
        return None


def _score(row):
    try:
        return float(row.get("match_score"))
    except (TypeError, ValueError):
        return None


def _level(row, jds):
    """The level, description first. Falls back to the title, exactly as web._build_row does."""
    jd = jds.get(row["url"]) if jds else None
    if jd:
        clean, _verdict = core.clean_jd(jd)
        lv = core.jd_level(clean)
        if lv:
            return lv, "stated"
    v = row.get("exp_max_years")
    if v not in (None, ""):
        try:
            lv = core.exp_level_for(int(v))
            if lv:
                return lv, "stated"
        except (TypeError, ValueError):
            pass
    lv = core.title_level(row.get("title") or "")
    return (lv, "inferred") if lv else ("", "")


def _pull_jds(urls, batch=400):
    got = {}
    for i in range(0, len(urls), batch):
        try:
            for r in db.load_jobs_by_urls(urls[i:i + batch], include_jd=True):
                if r.get("jd"):
                    got[r["url"]] = r["jd"]
        except Exception as exc:                       # a failed batch costs coverage, not the run
            print("  ! jd batch at %d failed: %s" % (i, type(exc).__name__))
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roles", default="pm,program,product",
                    help="comma-separated core.ROLE_KEYS to treat as the target")
    ap.add_argument("--cache", default=os.path.join(os.environ.get("TEMP", "."),
                                                    "pm_funnel_jds.json"))
    ap.add_argument("--no-jd", action="store_true",
                    help="skip the descriptions; the level then comes from the stored number "
                         "and the title only, which is what the CARD sees")
    args = ap.parse_args()

    want = tuple(k for k in core.parse_roles_pref(args.roles))
    assert want, "no valid role keys in %r" % args.roles
    labels = ", ".join(core.ROLE_LABELS[k] for k in want)

    print("backend: %s" % db.backend_name())
    rows = db.load_jobs(include_jd=False, cols=COLS)
    active = [r for r in rows if r.get("is_active") in (True, "true", 1, None)]
    target = [r for r in active if set(core.roles_for_title(r.get("title") or "")) & set(want)]

    jds = {}
    if not args.no_jd:
        if os.path.exists(args.cache):
            try:
                jds = json.load(io.open(args.cache, encoding="utf-8"))
                print("descriptions: %d from cache %s" % (len(jds), args.cache))
            except Exception:
                jds = {}
        if not jds:
            print("pulling %d descriptions..." % len(target))
            jds = _pull_jds([r["url"] for r in target])
            with io.open(args.cache, "w", encoding="utf-8") as fh:   # utf-8: see the docstring
                json.dump(jds, fh, ensure_ascii=False)
            print("descriptions: %d cached to %s" % (len(jds), args.cache))

    today = datetime.date.today()
    lv = {r["url"]: _level(r, jds) for r in target}
    entry = [r for r in target if lv[r["url"]][0] == "entry"]

    d = core.DEFAULT_PREFS
    print()
    print("THE FUNNEL -- role chips: %s" % labels)
    print("=" * 78)

    def step(name, rs):
        print("  %-56s %6d" % (name, len(rs)))
        return rs

    step("active corpus", active)
    s = step("+ hideagency=%s (default)" % d["hideagency"],
             [r for r in target if not (d["hideagency"] and core.is_agency(r.get("company") or ""))]
             if d["hideagency"] else target)
    s = step("+ the role chips", s)
    e = step("+ level == entry (READ FROM THE DESCRIPTION)",
             [r for r in s if lv[r["url"]][0] == "entry"])
    dd = step("+ default date=%s" % d["date"],
              [r for r in e if (lambda a: a is not None and a <= int(d["date"]))(_age(r, today))]
              if str(d["date"]).isdigit() else e)
    m = step("+ default match floor min=%s" % d["min"],
             [r for r in dd if (_score(r) is None or _score(r) >= d["min"])])
    print()
    print("  WHAT THE READER SEES ON DAY ONE: %d cards, from a real supply of %d." % (len(m), len(dd)))
    if dd:
        print("  The match floor alone costs %d of %d (%.0f%%)."
              % (len(dd) - len(m), len(dd), 100.0 * (len(dd) - len(m)) / len(dd)))
    print()
    print("  WHAT EACH OPTIONAL FILTER WOULD COST from those %d rows:" % len(dd))
    for name, keep in (
            # NOT the visa filter. It reads visa_tags, which web._build_row derives from
            # company_facts and the JD verdict -- neither is a column on `jobs`, so measuring
            # it here would report a confident 0% for every row. Measure that one through the
            # test client instead; a harness that answers a question it cannot see is the
            # exact defect this file exists to stop.
            ("any Minimum Pay value", lambda r: bool(r.get("salary_min"))),
            ("Remote only", lambda r: bool(r.get("remote"))),
            ("a single-state location filter (CA)", lambda r: (r.get("loc_state") or "") == "CA")):
        try:
            n = sum(1 for r in dd if keep(r))
        except Exception as exc:
            print("    %-38s (n/a: %s)" % (name, type(exc).__name__))
            continue
        print("    %-38s leaves %4d of %4d (%3.0f%%)" % (name, n, len(dd),
                                                         100.0 * n / max(1, len(dd))))
    print()
    print("  MATCH FLOOR SENSITIVITY, inside the date window:")
    print("    %-7s %10s %14s" % ("floor", "entry-level", "all target"))
    for f in (0, 20, 25, 30, 35, 40, 45, 50, 60):
        a = sum(1 for r in dd if (_score(r) is None or _score(r) >= f))
        b = sum(1 for r in s if (_score(r) is None or _score(r) >= f))
        print("    %-7d %10d %14d" % (f, a, b))
    print()
    print("  LEVEL COVERAGE on the %d target rows:" % len(s))
    tab = collections.Counter(lv[r["url"]][0] or "(cannot tell)" for r in s)
    src = collections.Counter(lv[r["url"]][1] or "(none)" for r in s)
    for k in ("entry", "mid", "senior", "(cannot tell)"):
        print("    %-14s %5d (%4.1f%%)" % (k, tab[k], 100.0 * tab[k] / max(1, len(s))))
    print("    source: description=%d  title=%d  neither=%d"
          % (src["stated"], src["inferred"], src["(none)"]))
    print()
    print("  REACHABILITY -- rows the role chips cannot show:")
    orphan = [r for r in active if not core.roles_for_title(r.get("title") or "")]
    print("    active rows with NO role family at all: %d (%.1f%%)"
          % (len(orphan), 100.0 * len(orphan) / max(1, len(active))))
    print()
    print("  TOP EMPLOYERS in the entry-level supply:")
    for co, n in collections.Counter(r.get("company") for r in e).most_common(12):
        print("    %4d  %s" % (n, co))


if __name__ == "__main__":
    main()
