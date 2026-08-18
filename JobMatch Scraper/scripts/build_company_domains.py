#!/usr/bin/env python3
"""build_company_domains.py - resolve every employer in the corpus to a real domain, by asking.

THE PROBLEM, measured. web.logodomain() falls back to "strip every non-alphanumeric from the
company name and append .com", and across 1,248 companies in the live corpus that fallback owns
76.9% of them. Probing 249 of the resulting domains found 22% of the guessed ones return 404 --
advancedmicrodevices.com, westinghouseelectriccompanyllc.com, accenturellp.com - each of which
renders as the letter monogram the feed is full of.

WHY GUESSING CANNOT BE FIXED BY A BETTER GUESS. Commit 83431be tried trusting the posting's own
host, measured it over every company, and found it changed 178 and regressed many of them: Udemy
became careerpuck.com, Capgemini talentnet.community. The rule was then narrowed to "accept the
host only when the name and the domain are prefixes of one another", which fixes .edu suffixes
and Inc/Corporation noise and nothing else. The missing ingredient was never a cleverer rule --
it was CHECKING. A domain either serves an icon or it does not, and that is one HTTP request.

So this generates candidates the same way, then VERIFIES each and keeps the first that answers.
The output, company_domains.json, is loaded by web.logodomain ahead of every rule.

    python scripts/build_company_domains.py                    # full run, writes the file
    python scripts/build_company_domains.py --limit 200        # a taste of it first
    python scripts/build_company_domains.py --only-missing     # keep what is resolved, do the rest

Writes nothing to the database. The only network traffic is the icon probes.
"""
import argparse
import collections
import json
import os
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db

OUT_PATH = "company_domains.json"
PROBE = ("https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON"
         "&fallback_opts=TYPE,SIZE,URL&size=64&url=http://%s")

# Hosts belonging to a hiring PLATFORM rather than to an employer. A posting on one of these
# says nothing about the company's own website. Kept in step with web._PLATFORM_HOSTS.
PLATFORM_HOSTS = (
    "myworkdayjobs.com", "myworkdaysite.com", "greenhouse.io", "lever.co", "ashbyhq.com",
    "smartrecruiters.com", "icims.com", "jobvite.com", "workable.com", "bamboohr.com",
    "taleo.net", "successfactors.com", "sapsf.com", "avature.net", "jobdiva.com", "ultipro.com",
    "paylocity.com", "oraclecloud.com", "eightfold.ai", "recruitics.com", "rippling.com",
    "isolvedhire.com", "apploi.com", "phenompeople.com", "peoplefluent.com", "silkroad.com",
    "brassring.com", "dayforcehcm.com", "adzuna.com", "indeed.com", "linkedin.com",
    "glassdoor.com", "ziprecruiter.com", "workatastartup.com", "applytojob.com", "myworkday.com",
    # Career-site front ends that are not obviously an ATS by name. careerpuck.com is the one
    # commit 83431be caught turning Udemy into careerpuck.com, and it answers an icon probe
    # perfectly happily -- verification cannot save you from a domain that really exists.
    "careerpuck.com", "talentnet.community", "comparably.com", "equest.com", "jobs.net",
    "smartrecruiters.net", "jazz.co", "jazzhr.com", "breezy.hr", "recruitee.com",
)

# Corporate suffixes that never appear in a domain. Longest first, so "corporation" is stripped
# before "corp" can match its head.
SUFFIXES = ("incorporated", "corporation", "technologies", "international", "limited",
            "holdings", "company", "group", "corp", "inc", "llc", "ltd", "plc", "lp", "co",
            "usa", "us", "na")

# Two-level public suffixes that need three labels to reach a registrable domain.
_SLD = ("co", "com", "ac", "org", "net", "gov", "edu")


def registrable(host):
    """The registrable domain of a hostname, or "" if it is a platform or unusable."""
    host = (host or "").lower().strip(".")
    if not host or any(p in host for p in PLATFORM_HOSTS):
        return ""
    labels = [p for p in host.split(".") if p]
    if len(labels) < 2:
        return ""
    n = 3 if (len(labels) >= 3 and labels[-2] in _SLD) else 2
    return ".".join(labels[-n:])


def candidates(name, hosts):
    """Domains worth trying for one company, best first, de-duplicated.

    Order is the whole design, and it is the opposite of what it looks like it should be -- see
    the comment below.
    """
    out = []
    key = (name or "").strip().lower()
    base = re.sub(r"[^a-z0-9]", "", key)

    # THE NAME COMES FIRST, and this order is the whole correctness argument.
    #
    # Trying the posting host first and keeping whatever answered produced udemy -> careerpuck.com
    # and amazon -> amazon.jobs on the first full run: an ATS host is a real domain with a real
    # icon, so verification cannot reject it. The employer's own name, when it resolves at all,
    # is the canonical answer -- amd.com and advancedmicrodevices.com return a byte-identical
    # icon because AMD owns both, which is exactly the property a brand domain has and a hiring
    # platform does not.
    #
    # The posting host is still valuable where the name resolves to nothing, so it stays as a
    # fallback, and a host that AGREES with the name (one is a prefix of the other, the rule
    # 83431be settled on) is promoted above the looser guesses.
    if base:
        out.append(base + ".com")
        trimmed = base
        for suf in SUFFIXES:
            if trimmed.endswith(suf) and len(trimmed) > len(suf) + 2:
                trimmed = trimmed[:-len(suf)]
                break
        if trimmed != base:
            out.append(trimmed + ".com")
        # Universities publish on .edu and essentially never on .com. There are 52 of them in
        # the corpus and the naive rule gets almost all of them wrong.
        if any(w in key for w in ("university", "college", "school of", "institute")):
            out.append((trimmed or base) + ".edu")
            words = [w for w in re.split(r"[^a-z0-9]+", key) if w]
            first = next((w for w in words if w not in ("the", "university", "of", "college")), "")
            if first:
                out.append(first + ".edu")

    # Posting hosts, agreeing-with-the-name ones first. Commonest host first within each group,
    # because a company with 700 postings on one domain and 2 on another is telling you which.
    agree, other = [], []
    for host, _n in collections.Counter(h for h in hosts if h).most_common():
        d = registrable(host)
        if not d:
            continue
        root = d.split(".")[0]
        (agree if (base and root and (root.startswith(base) or base.startswith(root)))
         else other).append(d)
    out.extend(agree)
    out.extend(other)

    seen, uniq = set(), []
    for d in out:
        if d and d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq[:7]              # a long tail of guesses is just noise on someone else's service


def alive(session, domain):
    """True when the icon service has a real icon for this domain.

    It answers 404 for an unknown domain - with a generic globe in the body that browsers never
    paint, because the status is not 2xx - so the status is the whole test. The length check
    catches a 200 carrying an empty body.
    """
    try:
        r = session.get(PROBE % domain, timeout=8)
        return r.status_code == 200 and len(r.content or b"") > 100
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="only the first N companies")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only-missing", action="store_true",
                    help="keep entries already in the output file and resolve only the rest")
    ap.add_argument("--out", default=OUT_PATH)
    a = ap.parse_args()

    import requests
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0 (JobMatch logo domain resolver)"

    print("reading companies from %s ..." % db.backend_name())
    rows = db._fetch_all(db.TABLE, {"select": "company,url", "order": "url"})
    by_company = collections.defaultdict(list)
    for r in rows:
        c = (r.get("company") or "").strip()
        if not c:
            continue
        try:
            by_company[c].append((urllib.parse.urlsplit(r.get("url") or "").hostname or "").lower())
        except Exception:
            pass
    print("  %d rows, %d distinct companies" % (len(rows), len(by_company)))

    known = {}
    if a.only_missing and os.path.exists(a.out):
        try:
            known = json.load(open(a.out, encoding="utf-8")).get("domains") or {}
            print("  %d already resolved; keeping them" % len(known))
        except Exception:
            known = {}

    names = sorted(by_company)
    if a.only_missing:
        names = [n for n in names if n.lower() not in known]
    if a.limit:
        names = names[:a.limit]
    print("  probing %d companies, %d workers\n" % (len(names), a.workers))

    def work(name):
        cands = candidates(name, by_company[name])
        for i, d in enumerate(cands):
            if alive(sess, d):
                return name, d, ("posting url" if i == 0 and d in
                                 {registrable(h) for h in by_company[name]} else "name")
        return name, "", "none"

    resolved = dict(known)
    stats = collections.Counter()
    naive_would_be = 0
    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for name, dom, how in ex.map(work, names):
            done += 1
            stats[how] += 1
            if dom:
                resolved[name.lower()] = dom
                if dom != re.sub(r"[^a-z0-9]", "", name.lower()) + ".com":
                    naive_would_be += 1
            if done % 100 == 0:
                print("  %5d/%d   resolved %d   nothing answered %d"
                      % (done, len(names), done - stats["none"], stats["none"]))

    print("\n  resolved from the posting URL : %d" % stats["posting url"])
    print("  resolved from the name        : %d" % stats["name"])
    print("  nothing answered              : %d" % stats["none"])
    print("  differ from the naive guess   : %d  (these are the ones that were 404ing)"
          % naive_would_be)

    payload = {"note": "company (lowercased) -> verified domain. Built by "
                       "scripts/build_company_domains.py; every value answered an icon probe.",
               "domains": resolved}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
    print("\n  wrote %s (%d entries)" % (a.out, len(resolved)))
    print("  DEPLOY IT - web.logodomain reads this file at import, so a map that stays on this "
          "machine changes nothing live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
