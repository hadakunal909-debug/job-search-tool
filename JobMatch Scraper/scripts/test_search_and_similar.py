#!/usr/bin/env python3
"""The search box forgives typos, ranks what you meant first, and the job page finds the same role
elsewhere.

Three things land here that no other test covers:

  * SEARCH MATCHING. scripts/feed_parity.py already proves web.py and app.js AGREE, which is a
    different claim from being RIGHT — two identical implementations of a broken matcher agree
    perfectly. This checks the behaviour itself: a transposition is forgiven, a three-letter term
    is not fuzzed at all, and a phrase still beats loose words.
  * SEARCH RANKING. searchRank decides what sits at the top, and the failure mode is silent: the
    right rows come back in an order that buries the one you typed.
  * SIMILAR ROLES. _similar_roles is title-space cosine over IDF weights, and its two failure
    modes are both quiet — an exact title scoring below the threshold (which is what an unsquared
    dot product does), and one employer's per-city duplicates filling all six slots.

Synthetic rows throughout: no Supabase, no résumé, runs anywhere.

    python scripts/test_search_and_similar.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["EV_OFF"] = "1"          # never write events from a test run

import web                          # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    if not cond:
        FAILS.append(name)
    print("  %s %-62s %s" % ("ok " if cond else "FAIL", name, extra))


print("=" * 92)
print("SEARCH: bounded edit distance")
print("=" * 92)
# Damerau, not plain Levenshtein: an adjacent swap has to cost 1, or none of the commonest typos
# reach their word at a 7-character term's tolerance of 1.
for a, b, k, want, why in [
    ("anaylst", "analyst", 1, True, "transposition — plain Levenshtein would charge 2"),
    ("amazno", "amazon", 1, True, "transposition"),
    ("enginer", "engineer", 1, True, "dropped letter"),
    ("manger", "manager", 1, True, "dropped letter"),
    ("sofware", "software", 1, True, "dropped letter"),
    ("capgemni", "capgemini", 2, True, "two edits, allowed at length 8"),
    ("analyst", "catalyst", 1, False, "two substitutions is too far at k=1"),
    ("xyz", "analyst", 2, False, "nothing alike"),
    ("data", "date", 1, True, "one substitution"),
]:
    check("_within(%-9s %-9s k=%d)" % (a, b, k), web._within(a, b, k) is want, why)

print("\nSEARCH: what counts as a match")
print("=" * 92)
HAY = "senior data scientist accenture boston, ma"
for q, want, why in [
    ("data scientist", True, "exact phrase"),
    ("data scientst", True, "one typo in one term"),
    ("scientst data", True, "terms out of order"),
    ("accentur", True, "typo in the employer"),
    ("boston", True, "location is searchable"),
    ("dat", True, "3-char PREFIX still matches by substring"),
    ("dta", False, "3-char TYPO does not: under the 4-char floor nothing is forgiven"),
    ("data manager", False, "every term must hit, not just one"),
    ("", True, "an empty query matches everything"),
]:
    check("searchHit(%r)" % q, web.searchHit(HAY, q) is want, why)
check("the 4-char floor is where forgiveness starts",
      web._search_tol("sql") == 0 and web._search_tol("data") == 1 and web._search_tol("engineer") == 2)

print("\nSEARCH: ranking puts what you typed on top")
print("=" * 92)
def row(t, c="Acme", loc="Boston, MA"):
    return {"title": t, "company": c, "location": loc}


q = "data scientst"
ranked = sorted(
    [row("Data Scientist"),
     row("Senior Data Scientist"),
     row("Staff Scientist - Real World Evidence and Data Strategy"),
     row("Program Manager", "Data Scientist Staffing Inc")],
    key=lambda r: -web.searchRank(r, q))
order = [r["title"] for r in ranked]
check("the plain role title ranks first", order[0] == "Data Scientist", " -> ".join(o[:26] for o in order))
check("a title that is mostly about something else ranks below both real ones",
      order.index("Staff Scientist - Real World Evidence and Data Strategy") >= 2)
check("a match in the EMPLOYER ranks below any title match",
      order[-1] == "Program Manager")
check("an exact phrase in the title outranks the same words scattered",
      web.searchRank(row("Project Manager"), "project manager")
      > web.searchRank(row("Manager, Special Projects"), "project manager"))
check("rank is 0 when nothing matches", web.searchRank(row("Line Cook"), "welder") == 0)

print("\nSIMILAR ROLES: title-space cosine")
print("=" * 92)
def jrow(t, c, url=None, score=50, closed=False):
    return {"title": t, "company": c, "url": url or ("https://x/%s/%s" % (c, t)).replace(" ", "-"),
            "score": score, "closed": closed, "sponsor_jd": "", "visa_likely": ""}


CORPUS = [
    jrow("Technical Project Manager", "Capgemini"),
    jrow("Technical Project Manager", "Ford", score=70),
    jrow("Senior Technical Project Manager", "Fiserv", score=60),
    jrow("Lead Technical Project Manager", "Disney", score=55),
    # One employer, same role, four cities: the corpus really is shaped like this, and without
    # de-duplication these alone would fill the rail.
    jrow("Technical Project Manager", "Actalent", url="https://x/act/1", score=90),
    jrow("Technical Project Manager", "Actalent", url="https://x/act/2", score=89),
    jrow("Technical Project Manager", "Actalent", url="https://x/act/3", score=88),
    jrow("Technical Project Manager", "Actalent", url="https://x/act/4", score=87),
    jrow("Registered Nurse", "Mayo Clinic"),
    jrow("Line Cook", "Aramark"),
    jrow("Technical Project Manager", "ClosedCo", closed=True),
    jrow("Another Role", "Capgemini"),
]
web._title_idx.update(fp=None, idf=None, toks=None)     # ignore any corpus-wide cache
src = CORPUS[0]
sim = web._similar_roles(src, CORPUS)
names = [(r["title"], r["company"]) for r in sim]
check("an identical title at another employer is found",
      ("Technical Project Manager", "Ford") in names, str(names))
check("the same employer is excluded (it has its own rail)",
      all(c != "Capgemini" for _t, c in names), str(names))
check("the posting being read is not in its own list", all(r["url"] != src["url"] for r in sim))
check("a closed posting is excluded", all(c != "ClosedCo" for _t, c in names))
check("an unrelated title is excluded",
      all(t not in ("Registered Nurse", "Line Cook") for t, _c in names), str(names))
check("one employer's per-city duplicates collapse to a single row",
      sum(1 for _t, c in names if c == "Actalent") == 1, str(names))
check("the best-scoring copy is the one kept",
      next((r for r in sim if r["company"] == "Actalent"), {}).get("score") == 90)
# The bug that made every exact match fall under the threshold: the dot product of two
# idf-weighted vectors is the sum of idf SQUARED, and summing plain idf scored identical titles
# at 0.30 instead of 1.00.
idf, toks = web._title_index(CORPUS)
import math                                             # noqa: E402
mine = toks[src["url"]]
n2 = sum(idf[t] ** 2 for t in mine)
check("two identical titles score exactly 1.0",
      abs(sum(idf[t] ** 2 for t in mine) / (math.sqrt(n2) * math.sqrt(n2)) - 1.0) < 1e-9)
check("a single shared word is not enough once titles get long",
      not web._similar_roles(jrow("Software Engineer, ML Tech Transfer", "Adobe"),
                             CORPUS + [jrow("Paying Transfer Agent Operations Specialist", "Bank")]))

print()
if FAILS:
    print("FAILURES (%d):" % len(FAILS))
    for f in FAILS:
        print("   %s" % f)
    raise SystemExit(1)
print("ALL SEARCH + SIMILAR-ROLE CHECKS PASS")
