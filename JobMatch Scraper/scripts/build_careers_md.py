#!/usr/bin/env python3
"""
build_careers_md.py — rewrite careers_us.md's status markers from SOURCES.

careers_us.md is a SHIPPED RUNTIME ASSET (web.py opens it relative to cwd for /careers, and
build_deploy_zip.py packs it), not documentation. It is the page you browse to find an employer
to apply to directly, so the one thing it must get right is whether the scraper already covers
that employer -- if it does, the job is in the feed; if it does not, that careers link is the
only way you will see the role.

It got that wrong for months. A hand-written section headed "Companies currently in SOURCES"
listed 26 employers while 145 of the page's 289 were actually scraped, so the page understated
its own coverage by nearly 6x. Hand-maintained status next to a list that changes every time a
board is added can only drift, hence this script. Run it after touching SOURCES:

    python scripts/build_careers_md.py            # rewrite in place
    python scripts/build_careers_md.py --check    # exit 1 if stale, for CI

It also fixes markup that never rendered. web.py::_md_to_html is a deliberately tiny renderer
-- headings, list items and links, nothing else -- and it html-escapes before linkifying. So
`**Name**` displayed as literal asterisks and `&middot;` displayed as the literal text
"&middot;". Both are rewritten to what the renderer can actually show.

Idempotent: parses the name out of either the old `**Name**` form or the current plain form.

--check is deliberately NOT a CI gate. 153 boards live only in the `boards` table (added
through /add) and never appear in SOURCES, so the marks depend on whether the run could reach
a database. As measured 2026-08-22 that changes nothing -- every employer NAMED on this page
that we cover is covered via SOURCES, and the boards table adds 0 marks -- but it is one /add
away from mattering, and then a CI run with no credentials would call a correctly generated
file stale. Regenerate from a machine that can reach the database; the counts it prints say
whether it did.
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper

DOC = "careers_us.md"
IN_FEED = "in feed"
DIRECT = "apply direct"
# Deliberately non-overlapping as substrings, because /careers' search box filters whole lines:
# typing "apply direct" lists exactly the gaps, "in feed" exactly the covered ones. Overlapping
# words ("tracked" / "untracked") would make one of those searches useless.
assert IN_FEED not in DIRECT and DIRECT not in IN_FEED

# The page and SOURCES label the same employer differently. Same-company aliases only -- a
# parent whose board does NOT carry the subsidiary's postings does not belong here.
ALIAS = {
    "Amazon Web Services": "Amazon",
    "Meta Platforms": "Meta",
    "Micron Technology": "Micron",
    "Advanced Micro Devices": "AMD",
    "Walmart Global Tech": "Walmart",
    "Ernst & Young": "EY",
    "Dell": "Dell Technologies",
    "Dell EMC": "Dell Technologies",
    "Regeneron Pharmaceuticals": "Regeneron",
    "W.W. Grainger": "Grainger",
    "UCSF Medical Center": "University of California, San Francisco",
    "Iris Software": "IRIS Software",
    # Covered only through a parent's board. Kept separate in intent but marked as covered,
    # because the postings really do arrive: Genentech's are on Roche's Workday site,
    # Janssen's on Johnson & Johnson's, and Seagen was acquired by Pfizer.
    "Genentech": "Roche",
    "Janssen Research & Development": "Johnson & Johnson",
    "Seagen": "Pfizer",
    "Booking.com": "Booking Holdings",
}

LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
# Either the original `- **Name** &middot; ...` or this script's own `- Name · ...` output.
ENTRY = re.compile(r"^-\s+(?:\*\*(?P<b>[^*]+)\*\*|(?P<p>[^·]+?))\s*(?:&middot;|·)\s*(?P<rest>.*)$")


def _norm(s):
    s = (s or "").lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def _scraped_names():
    """Normalised employer names we actually scrape: the built-in list plus the boards table.

    custom_sources() is included because a board added through /add lives ONLY in that table,
    and a page that called such an employer uncovered would be wrong in the direction that
    costs the most -- telling you to go apply somewhere the feed already watches.
    """
    names = {_norm(r[2]) for r in scraper.SOURCES}
    builtin = len(names)
    try:
        names |= {_norm(c) for _u, _a, c in (scraper.custom_sources() or [])}
    except Exception as exc:              # no database here (CI, or a laptop without creds)
        sys.stderr.write("note: boards table unavailable (%s); built-in SOURCES only, so "
                         "employers added via /add will be marked \"%s\" and this file will "
                         "UNDER-report coverage.\n" % (type(exc).__name__, DIRECT))
        return names
    print("employers: %d in SOURCES + %d only in the boards table."
          % (builtin, len(names) - builtin))
    return names


def rewrite(text, scraped):
    lines = text.split("\r\n")
    out, counts, first_section = [], {IN_FEED: 0, DIRECT: 0}, True
    for ln in lines:
        m = ENTRY.match(ln)
        if not m:
            if ln.startswith("## ") and first_section:
                first_section = False
                # Was "Companies currently in SOURCES", which is what went stale. What these
                # 26 actually share is an ATS with a public JSON feed, which is why they were
                # the easy ones to add -- a fact that stays true as coverage changes.
                out.append("## On a public ATS (Greenhouse / Lever / Ashby / SmartRecruiters)")
                continue
            out.append(ln)
            continue
        name = (m.group("b") or m.group("p") or "").strip()
        # Drop any status this script wrote before, so re-running cannot stack them up.
        rest = m.group("rest")
        for tag in (IN_FEED, DIRECT):
            rest = re.sub(r"^%s\s*(?:&middot;|·)\s*" % re.escape(tag), "", rest)
        links = LINK.findall(rest)
        status = IN_FEED if _norm(ALIAS.get(name, name)) in scraped else DIRECT
        counts[status] += 1
        parts = [name, status] + ["[%s](%s)" % (t, u) for t, u in links]
        out.append("- " + " · ".join(parts))

    total = counts[IN_FEED] + counts[DIRECT]
    head = [
        "# US career-page links for H-1B sponsor companies",
        "",
        "%d employers. %d are marked \"%s\" -- the scraper reads that company's board, so its "
        "roles reach the feed on their own. The other %d are marked \"%s\": there is no board "
        "for them, so the careers link is the only way you will see those roles."
        % (total, counts[IN_FEED], IN_FEED, counts[DIRECT], DIRECT),
        "",
        "Search this page for \"%s\" to list just the gaps, or \"%s\" for just the covered "
        "employers. Every entry also carries a United-States-filtered LinkedIn search, which "
        "is the reliable way to see only US roles. Native careers links are best-effort: if "
        "one redirects, use the LinkedIn link."
        % (DIRECT, IN_FEED),
    ]
    body = out[next(i for i, l in enumerate(out) if l.startswith("## ")):]
    return "\r\n".join(head + [""] + body), counts


def main():
    if not os.path.exists(DOC):
        sys.exit("%s not found -- run this from the app directory, not the repo root." % DOC)
    raw = open(DOC, "rb").read()
    text = raw.decode("utf-8")
    if "\n" in text.replace("\r\n", ""):
        sys.exit("%s has bare LFs; refusing to rewrite and normalise the whole file." % DOC)

    new, counts = rewrite(text, _scraped_names())
    if "--check" in sys.argv:
        if new.encode("utf-8") != raw:
            sys.exit("%s is stale -- run python scripts/build_careers_md.py" % DOC)
        print("ok: %s is current (%d %s, %d %s)."
              % (DOC, counts[IN_FEED], IN_FEED, counts[DIRECT], DIRECT))
        return
    if new.encode("utf-8") == raw:
        print("unchanged: %s (%d %s, %d %s)."
              % (DOC, counts[IN_FEED], IN_FEED, counts[DIRECT], DIRECT))
        return
    open(DOC, "wb").write(new.encode("utf-8"))
    print("wrote %s: %d %s, %d %s (of %d)."
          % (DOC, counts[IN_FEED], IN_FEED, counts[DIRECT], DIRECT,
             counts[IN_FEED] + counts[DIRECT]))


if __name__ == "__main__":
    main()
