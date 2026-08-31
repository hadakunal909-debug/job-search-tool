#!/usr/bin/env python3
"""
classify_everify.py — categorise the federal E-Verify+ employer list before anything is scraped.

The USCIS download ("Employer List_data.csv", 35,272 rows as of the 2026-08-15 snapshot) looks
like a corporate registry and is not one. Every row is Account Status=Open, Opted into
E-Verify+=Yes and enrolled in 2026, so those three columns carry no information; and `Employer`
is free text an HR person typed at enrolment, so "Amazon" is a single AZ hiring site and the
Department of Veterans Affairs appears under ten spellings. 34,892 distinct strings collapse to
only 34,601 after normalisation — there is no company identity to key on.

So we do NOT try to classify by name. We classify by joining against the federal filing data we
already have (sponsor_counts.json), and use the name only for a coarse industry read. Output is
two review artifacts; the user cherry-picks from them.

    everify_categorised.csv   one row per input row, tagged with bucket/category/filings
    everify_insights.md       the aggregate picture, regenerated from the actual run

REVIEW-FIRST and OFFLINE: no network calls, and nothing outside those two files is written.
SOURCES is never touched — see probe_everify_xlsx.py for the (also review-first) probe step.

    python -m scraper.classify_everify
    python -m scraper.classify_everify --csv "C:/path/to/Employer List_data.csv"
    python -m scraper.classify_everify --xlsx "C:/path/to/Employer List.xlsx" --out-dir .
"""
import os
import re
import sys
import csv
import collections

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import scraper
from scraper.probe_everify_xlsx import read_xlsx, _size_lower, NONTARGET

DEFAULT_CSV = r"C:\Users\k.signhhada\Downloads\Employer List_data.csv"
REPORT_CSV = "everify_categorised.csv"
REPORT_MD = "everify_insights.md"

# A scrape candidate needs enough headcount to plausibly run a real ATS. Below this the
# sponsors are overwhelmingly IT body-shops filing against a handful of staff.
CANDIDATE_MIN_STAFF = 100

# Coarse industry read. First match wins, so order matters: the narrow public-sector and
# education patterns must be tried before the broad healthcare/IT ones. This reaches only
# ~1/3 of the list by design — see the "unclassified" note in the generated insights.
TAXONOMY = (
    ("gov_federal", r"\b(department of (veterans|energy|the interior|homeland|state|labor|"
                    r"justice|defense)|u\.?s\.? (army|navy|air force|coast guard|marine)|"
                    r"federal bureau|internal revenue|\birs\b|\bepa\b|nasa|veterans (affairs|"
                    r"health)|vamc|defense finance|social security)\b"),
    ("gov_state_local", r"\b(city of|state of|county (of|public)|township|municipal|sheriff|"
                        r"police dep|fire dep|public works|department of (public health|social|"
                        r"transportation|corrections|family))\b"),
    ("education_k12", r"\b(school district|public schools|elementary|middle school|high school|"
                      r"academy|charter school|preschool|montessori|childcare|child care|"
                      r"daycare|learning center|head start)\b"),
    ("education_higher", r"\b(university|college|institute of technology|community college|"
                         r"seminary|school of (medicine|law|business))\b"),
    ("healthcare", r"\b(health|hospital|medical|clinic|dental|dentist|nursing|rehab|care|"
                   r"hospice|pharmacy|physician|surgery|surgical|orthoped|pediatric|therapy|"
                   r"therapist|behavioral|psychiat|veterinary|senior living|assisted living|"
                   r"home care|caregiv)\b"),
    ("staffing_agency", r"\b(staffing|recruit|recruiting|recruitment|talent|employment agency|"
                        r"temp agency|manpower|placement|headhunt|peo\b|workforce solutions|"
                        r"personnel)\b"),
    ("it_consulting", r"\b(infotech|info tech|it solutions|it services|softech|soft tech|"
                      r"systems inc|technolog|software|consultanc|consulting|cybersecur|"
                      r"data (systems|solutions)|digital|analytics|cloud|labs?\b|tech\b)\b"),
    ("construction", r"\b(construction|contracting|contractors|builders|building|roofing|"
                     r"plumbing|electric|hvac|concrete|paving|landscap|excavat|drywall|"
                     r"masonry|remodel|renovat|carpentry|welding|framing)\b"),
    ("food_hospitality", r"\b(restaurant|pizza|cafe|coffee|grill|bbq|catering|food|kitchen|"
                         r"bakery|donut|taco|burger|sushi|deli|brewing|hotel|motel|inn\b|"
                         r"resort|hospitality|mcdonald|subway|dunkin|chick-fil|wendy|domino|"
                         r"7-eleven|7 eleven)\b"),
    ("retail_auto", r"\b(store|market|mart\b|grocery|retail|shop\b|boutique|auto|automotive|"
                    r"motors|car wash|dealership|tire|collision|body shop)\b"),
    ("logistics_transport", r"\b(trucking|transport|logistics|freight|carrier|delivery|courier|"
                            r"moving|warehouse|distribution|fleet|hauling)\b"),
    ("prof_services", r"\b(law (firm|office)|attorney|legal|accounting|cpa\b|tax service|"
                      r"insurance|realty|real estate|property manage|mortgage|financial|bank|"
                      r"credit union|advisor|architect|engineering)\b"),
    ("cleaning_facilities", r"\b(cleaning|janitorial|maid|housekeep|facilit|maintenance|"
                            r"pest control|security service|guard|landscaping|lawn)\b"),
    ("manufacturing", r"\b(manufactur|industries|industrial|fabricat|machine|machining|"
                      r"tool & die|foundry|plastics|steel|metal|packaging|assembly|mill\b)\b"),
    ("nonprofit_faith", r"\b(church|ministry|ministries|catholic|baptist|methodist|synagogue|"
                        r"mosque|temple|foundation|charit|nonprofit|non-profit|"
                        r"community (center|service)|ymca|salvation army|goodwill|united way)\b"),
)
_TAXONOMY = tuple((name, re.compile(pat, re.I)) for name, pat in TAXONOMY)

def _sources_key(s):
    """Normalisation used for the SOURCES comparison. _strict_norm_name, not _norm_name:
    the aggressive stripper turns 'Target Labs INC' into 'target' and would report that a
    five-person Virginia company is one we already scrape."""
    return scraper._strict_norm_name(s)


def categorise(name, dba=""):
    """Coarse industry bucket from the employer/DBA text, or 'unclassified'."""
    blob = "%s %s" % (name, dba or "")
    for label, rx in _TAXONOMY:
        if rx.search(blob):
            return label
    return "unclassified"


def build_sources_matcher(extra=()):
    """Return match(name) -> the company name we already scrape, or ''.

    `extra` folds in names that are scraped but are NOT in the static SOURCES list -- in
    practice the `boards` DB table, which is where every adopted board lives. Without it this
    answers "" for Cognizant and Deloitte (73 and 302 live jobs, both adopted into `boards`),
    and discover_companies then queues them for a probe we do not need. SOURCES-only was the
    original scope because the E-Verify classifier had no DB handle; callers that do should
    pass one.

    Exact equality alone undercounts badly: the federal list says "Marriott International" and
    "BLACKROCK FINANCIAL MANAGEMENT INC" where SOURCES says "Marriott" and "BlackRock". So we
    also accept a SOURCES name that is a whole-word PREFIX of the federal name — the same
    idiom build_everify.py already uses to match a snapshot against our vetted set.

    Prefix, not token containment: containment in either direction matches on any shared
    distinctive-looking word, which paired "IT AMERICA INC" with "Samsung Research America"
    and "Boston Inc." with "Boston Medical Center".

    Short single-word SOURCES names stay inherently ambiguous under a prefix rule ("Target"
    still claims "Target Labs INC"). That only mislabels the sources_name column of rows that
    are not sponsors anyway, and the CSV exists to be eyeballed, so it is left alone.
    """
    by_key = {}
    for company in [c for _u, _a, c in scraper.SOURCES] + list(extra):
        if not company:
            continue
        k = _sources_key(company)
        if k:
            by_key.setdefault(k, company)
    prefixes = sorted(by_key)                       # longest match wins, checked below

    def match(name):
        n = _sources_key(name)
        if not n:
            return ""
        if n in by_key:
            return by_key[n]
        best = ""
        for k in prefixes:
            if n.startswith(k + " ") and len(k) > len(best):
                best = k
        return by_key[best] if best else ""

    return match


def load_rows(path):
    """[{employer, dba, size, state, sites}, ...] from the USCIS csv or xlsx export."""
    if path.lower().endswith(".csv"):
        with open(path, encoding="utf-8-sig", newline="") as f:
            raw = list(csv.DictReader(f))
        get = lambda r, k: (r.get(k) or "").strip()          # noqa: E731
    else:
        header, body = read_xlsx(path)
        idx = {h: i for i, h in enumerate(header)}
        raw = [{h: (r[i] if i < len(r) else "") for h, i in idx.items()} for r in body]
        get = lambda r, k: str(r.get(k) or "").strip()       # noqa: E731
    out = []
    for r in raw:
        name = get(r, "Employer")
        if not name:
            continue
        out.append({"employer": name,
                    "dba": get(r, "Doing Business As"),
                    "size": get(r, "Workforce Size"),
                    "state": get(r, "Hiring Site Locations"),
                    "sites": get(r, "Number of Hiring Sites"),
                    "status": get(r, "Account Status")})
    return out


def classify(rows, counts):
    """Tag every row with category / bucket / filings. Returns the same list, mutated."""
    in_sources = build_sources_matcher()
    for r in rows:
        n, method = scraper._safe_sponsor_match(r["employer"], counts, r["size"], r["dba"])
        r["h1b_filings"] = n
        r["match_method"] = method
        r["sources_name"] = in_sources(r["employer"])
        r["in_sources"] = "yes" if r["sources_name"] else "no"
        r["category"] = categorise(r["employer"], r["dba"])
        r["bodyshop"] = "yes" if core.BODYSHOP_RE.search(r["employer"]) else "no"
        r["public_sector"] = "yes" if NONTARGET.search(r["employer"]) else "no"
        staff = _size_lower(r["size"])
        if not n:
            r["bucket"] = "4_no_scope"
        elif r["sources_name"]:
            r["bucket"] = "1_already_have"
        elif staff >= CANDIDATE_MIN_STAFF:
            r["bucket"] = "2_candidate"
        else:
            r["bucket"] = "3_small_sponsor"
    return rows


# ------------------------------- reporting -------------------------------
BUCKET_LABEL = {
    "1_already_have": "Already scraped (sponsor already in SOURCES)",
    "2_candidate": "NEW - worth scraping (sponsor, 100+ staff)",
    "3_small_sponsor": "NEW but out of scope (sponsor, <100 staff)",
    "4_no_scope": "No scope (no H-1B filing record)",
}


def _pct(n, d):
    return (n * 100.0 / d) if d else 0.0


def write_csv(rows, path):
    cols = ["employer", "dba", "size", "state", "sites", "bucket", "category",
            "h1b_filings", "match_method", "in_sources", "sources_name",
            "bodyshop", "public_sector"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r["bucket"], -r["h1b_filings"],
                                             r["employer"].lower())):
            w.writerow({k: r.get(k, "") for k in cols})


def write_insights(rows, path, src):
    total = len(rows)
    buckets = collections.Counter(r["bucket"] for r in rows)
    sponsors = [r for r in rows if r["h1b_filings"]]
    implausible = [r for r in rows if r["match_method"] == "implausible"]
    cands = [r for r in rows if r["bucket"] == "2_candidate"]
    L = []
    A = L.append
    A("# E-Verify+ employer list — categorisation\n")
    A("Generated by `scraper/classify_everify.py` from `%s`.\n" % os.path.basename(src))
    A("Offline run: the only inputs are the export and `sponsor_counts.json`.\n")

    A("\n## What the file is\n")
    stat = collections.Counter(r["status"] for r in rows)
    A("- Rows: **%d**, of which distinct employer strings: %d (normalised: %d)"
      % (total, len({r["employer"] for r in rows}),
         len({scraper._norm_name(r["employer"]) for r in rows})))
    A("- `Account Status` values: %s" % ", ".join("`%s` %d" % kv for kv in stat.most_common()))
    A("- Near-total name uniqueness means there is no company identity to key on; the sponsor "
      "join below is the only reliable discriminator.\n")

    A("\n## Buckets\n")
    A("| Bucket | Count | Share |")
    A("|---|---:|---:|")
    for b in sorted(BUCKET_LABEL):
        A("| %s | %d | %.1f%% |" % (BUCKET_LABEL[b], buckets.get(b, 0),
                                    _pct(buckets.get(b, 0), total)))
    A("| **TOTAL** | **%d** | 100.0%% |" % total)

    A("\n## Does E-Verify+ predict sponsorship?\n")
    A("- Rows with a size-plausible H-1B record: **%d (%.1f%%)**"
      % (len(sponsors), _pct(len(sponsors), total)))
    A("- Rows rejected as implausible collisions: %d" % len(implausible))
    A("\nBeing on this list is close to no evidence of sponsorship. Sponsor rate tracks "
      "*headcount*, not E-Verify+ enrolment:\n")
    A("| Workforce | Rows | Sponsors | Rate |")
    A("|---|---:|---:|---:|")
    bands = sorted({r["size"] for r in rows}, key=_size_lower, reverse=True)
    for band in bands:
        sub = [r for r in rows if r["size"] == band]
        sp = [r for r in sub if r["h1b_filings"]]
        A("| %s | %d | %d | %.1f%% |" % (band or "(blank)", len(sub), len(sp),
                                         _pct(len(sp), len(sub))))

    A("\n## Industry read\n")
    A("Regex over the name field. It reaches only part of the list by design — the rest are "
      "idiosyncratic small-business names that no keyword set will classify.\n")
    A("| Category | Rows | Share | Sponsor rate |")
    A("|---|---:|---:|---:|")
    cats = collections.Counter(r["category"] for r in rows)
    for cat, n in cats.most_common():
        sp = sum(1 for r in rows if r["category"] == cat and r["h1b_filings"])
        A("| %s | %d | %.1f%% | %.1f%% |" % (cat, n, _pct(n, total), _pct(sp, n)))

    A("\n## Bucket 2 — the actual work queue\n")
    A("%d companies. Split by size:\n" % len(cands))
    big = [r for r in cands if _size_lower(r["size"]) >= 500]
    A("- 500+ staff: **%d**" % len(big))
    A("- 100-499 staff: **%d**" % (len(cands) - len(big)))
    A("\n| H-1B filings | Employer | Workforce | States |")
    A("|---:|---|---|---|")
    for r in sorted(cands, key=lambda r: -r["h1b_filings"])[:40]:
        A("| %d | %s | %s | %s |" % (r["h1b_filings"], r["employer"], r["size"], r["state"]))

    A("\n## Bucket 4 — why it is not a backlog\n")
    nos = [r for r in rows if r["bucket"] == "4_no_scope"]
    A("- %d rows with no H-1B record at all." % len(nos))
    A("- Of those, %d match the public-sector pattern (school districts, cities, federal "
      "agencies). Federal employers cannot sponsor H-1B and generally require citizenship, so "
      "they are negative-value rows, not untapped ones."
      % sum(1 for r in nos if r["public_sector"] == "yes"))
    A("- %d match the body-shop name heuristic."
      % sum(1 for r in nos if r["bodyshop"] == "yes"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def _arg(flag, default=None, cast=str):
    if flag in sys.argv:
        try:
            return cast(sys.argv[sys.argv.index(flag) + 1])
        except (ValueError, IndexError):
            print("%s needs a value" % flag)
            sys.exit(1)
    return default


def main():
    src = _arg("--csv") or _arg("--xlsx") or DEFAULT_CSV
    out_dir = _arg("--out-dir", ".")
    if not os.path.exists(src):
        print("input not found:", src)
        return 1

    counts = core.load_sponsor_counts()
    if not counts:
        print("sponsor_counts.json missing or empty — every row would land in bucket 4.")
        return 1

    rows = load_rows(src)
    print("read %d rows from %s" % (len(rows), os.path.basename(src)), flush=True)
    classify(rows, counts)

    csv_path = os.path.join(out_dir, REPORT_CSV)
    md_path = os.path.join(out_dir, REPORT_MD)
    write_csv(rows, csv_path)
    write_insights(rows, md_path, src)

    buckets = collections.Counter(r["bucket"] for r in rows)
    print()
    for b in sorted(BUCKET_LABEL):
        print("  %-52s %6d" % (BUCKET_LABEL[b], buckets.get(b, 0)))
    print("  %-52s %6d" % ("TOTAL", len(rows)))
    print("\nwrote %s" % os.path.abspath(csv_path))
    print("wrote %s" % os.path.abspath(md_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
