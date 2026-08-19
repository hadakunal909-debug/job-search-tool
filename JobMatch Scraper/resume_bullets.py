"""
resume_bullets.py — per-bullet review: which of the three parts a line has, and what to add.

`resume_score.py` grades the DOCUMENT. It can tell you 6 of 15 bullets carry no number, and it can
highlight them, but it cannot tell you what to write instead — so the user is left with a correct
diagnosis and no next move. This module closes that gap, and it is the piece Resume Worded actually
sells: they score each bullet, not just the résumé.

Their model, restated: a bullet is **past-tense action verb + scope + measurable result**. Three
parts, and a line is weak in a specific way depending on which one is missing. That framing is worth
copying because it turns "this bullet is weak" into one of three concrete instructions.

Everything here is rules and lexicons — no model, no network. Two data sets do the work:

  * `_VERB_SWAPS`, weak opener -> the strong verbs that replace it. Not a generic verb list: the
    replacement depends on what the weak verb was hiding. "Assisted" wants Prepared/Processed/
    Resolved (you did a thing), "Participated in" wants Proposed/Led/Delivered (you drove a thing).
  * `_METRIC_HINTS`, bullet topic -> the metric TYPES that fit it. Generic advice ("add a number")
    is what makes résumé tools useless; a bullet about vendor contracts should be asked for dollars
    saved and percent under budget, and one about dashboards for hours removed and audience size.
    The categories come from the standard taxonomy: money, percentage change, time saved, scale,
    volume of work, cadence.

Deliberately NOT here: rewriting the bullet. That needs a model, it is the one thing this project
said no to, and a canned rewrite of someone's real accomplishment would be worse than none.
"""
import re

import resume_score as rs

# Weight of each part, summing to 10. The verb is worth most because a bullet that opens weakly is
# read as a duty no matter what follows it, and the result is worth more than the scope because a
# number is what turns a claim into evidence.
PART_WEIGHTS = {"verb": 4.0, "result": 3.5, "scope": 2.5}

_VERB_SWAPS = {
    "responsible": ("Managed", "Ran", "Owned"),
    "helped": ("Built", "Drafted", "Coordinated", "Analyzed"),
    "help": ("Built", "Drafted", "Coordinated"),
    "assisted": ("Prepared", "Processed", "Resolved", "Presented"),
    "assist": ("Prepared", "Processed", "Resolved"),
    "participated": ("Proposed", "Led", "Delivered"),
    "participate": ("Proposed", "Led", "Delivered"),
    "worked": ("Built", "Shipped", "Drove"),
    "work": ("Built", "Shipped", "Drove"),
    "supported": ("Enabled", "Ran", "Maintained", "Resolved"),
    "support": ("Enabled", "Ran", "Maintained"),
    "involved": ("Led", "Delivered", "Drove"),
    "contributed": ("Built", "Delivered", "Wrote"),
    "handled": ("Resolved", "Processed", "Owned"),
    "handle": ("Resolved", "Processed", "Owned"),
    "tasked": ("Owned", "Led", "Delivered"),
    "duties": ("Owned", "Ran"),
    "dealt": ("Resolved", "Negotiated"),
    "attended": ("Presented", "Represented", "Contributed"),
    "aided": ("Enabled", "Prepared", "Resolved"),
    "used": ("Built", "Automated", "Delivered"),
    "using": ("Built", "Automated", "Delivered"),
    "utilized": ("Built", "Automated", "Applied"),
    "utilised": ("Built", "Automated", "Applied"),
    "learned": ("Applied", "Delivered", "Built"),
    "shadowed": ("Supported", "Prepared"),
    "observed": ("Documented", "Analyzed"),
    "engaged": ("Partnered", "Negotiated", "Won"),
    "exposed": ("Applied", "Delivered"),
    "various": ("<name the actual work>",),
    "numerous": ("<name the actual work>",),
    "multiple": ("<name the actual work>",),
}

# Topic -> metric types that fit it. Ordered; the first match wins, so the more specific patterns
# come first. Each entry is (pattern, [suggestions]).
_METRIC_HINTS = (
    (r"revenue|sales|pipeline|quota|bookings|upsell|deal",
     ("dollars of revenue or pipeline", "percent growth against the prior period",
      "number of deals or accounts")),
    (r"cost|vendor|contract|budget|spend|procure|invoice|purchas|licen[cs]e",
     ("dollars saved or avoided", "percent under budget or against prior-year spend",
      "total spend or number of contracts you were accountable for")),
    (r"report|dashboard|analytic|\bkpi|metric|visuali|tableau|power bi|looker",
     ("how many reports or dashboards", "hours of manual work removed per week or month",
      "how many people or teams relied on it")),
    (r"automat|script|pipeline|etl|integrat|migrat|deploy|ci[/ -]?cd",
     ("hours saved per week", "percent reduction in runtime or manual steps",
      "how many records, jobs or systems it covered")),
    (r"team|mentor|train|supervis|coach|onboard|hire|recruit|staff",
     ("how many people", "how many were promoted, retained or certified",
      "how many teams, shifts or sites")),
    (r"customer|client|patient|student|user|member|tenant|guest",
     ("how many served per week or month", "satisfaction, retention or NPS change",
      "response or resolution time before and after")),
    (r"process|workflow|operation|efficien|cycle|throughput|lean|six sigma",
     ("percent cycle-time reduction", "volume handled per day, week or month",
      "hours saved, or headcount freed for other work")),
    (r"audit|complian|risk|control|quality|defect|error|safety|regulat",
     ("error, defect or exception rate before and after", "how many findings closed",
      "percent compliance or pass rate")),
    (r"project|program|initiative|launch|rollout|implementat|deliver|milestone",
     ("how many projects or workstreams", "percent delivered on time or in scope",
      "budget size and how many stakeholders")),
    (r"inventory|logistic|supply|warehouse|procurement|shipment|fleet|asset",
     ("units, SKUs or shipments", "percent accuracy or shrinkage change",
      "cost per unit, or dollars of inventory")),
    (r"data|model|forecast|machine learning|\bml\b|python|sql|statistic",
     ("records or rows processed", "accuracy, error or latency change as a percent",
      "how many sources, tables or downstream consumers")),
    (r"market|campaign|content|brand|seo|social|event|outreach",
     ("reach, impressions or attendees", "conversion or engagement rate change",
      "how many campaigns or pieces shipped")),
)
_METRIC_HINTS = tuple((re.compile(p, re.I), s) for p, s in _METRIC_HINTS)

# Fallback when no topic matches. This is the taxonomy itself rather than "add a number", plus the
# standard ways to find one when you think you have none — which is the objection every user has.
_GENERIC_METRICS = ("a percentage change", "a dollar amount", "a count or volume",
                    "time saved per week")
FIND_A_NUMBER = (
    "Estimate it — your best honest guess beats no number, and a range is fine.",
    "Derive it: hours saved times an hourly rate gives a dollar figure.",
    "State the scale instead: how large was the thing you worked on.",
    "State the cadence: how often you did it — daily, weekly, per quarter.",
    "Count the people: served, supported, trained, or on the team.",
)

# A RESULT is an outcome you can point at. Five shapes, and the last three were missing at first —
# the regex only accepted a percentage, a currency amount, or a change verb beside a digit, so
# "Automated 240 hours per quarter" and "two of whom were promoted" both read as having no result.
# Time saved and quantity of work are metrics in the taxonomy; leaving them out meant the check told
# a well-written bullet it was a duty, which is the most expensive kind of wrong.
_NUM = r"(?:\d[\d,.]*|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozens?)"
_OUTCOME = (r"promot|retain|certif|clos|resolv|ship|launch|won|win|retir|eliminat|avert|prevent|"
            r"onboard|convert|renew|deliver")
_RESULT_RE = re.compile(
    # 1. percentage or currency
    r"\d+\s*%|%\s*\d+|[$£€]\s?\d"
    # 2. magnitude
    r"|\b\d+(?:\.\d+)?\s?(?:k|m|bn|billion|million|thousand)\b"
    # 3. a change verb near a number
    r"|\b(?:reduc|increas|cut|sav|grew|grow|improv|lift|rais|lower|shorten|acceler|doubl|tripl|"
    r"eliminat|boost|automat|streamlin|consolidat)\w*\b[^.;]{0,40}?\d"
    # 4. time saved or spent per period -- "240 hours per quarter", "4 days", "2 FTE"
    r"|\b" + _NUM + r"\s*(?:hours?|hrs?|days?|weeks?|months?|minutes?|mins?|ftes?)\b"
    # 5. a countable outcome, in either order
    r"|\b(?:" + _OUTCOME + r")\w*\b[^.;]{0,30}?\b" + _NUM + r"\b"
    r"|\b" + _NUM + r"\b[^.;]{0,30}?\b(?:" + _OUTCOME + r")\w*"
    # 6. an explicit before/after
    r"|\bfrom\s+\d[\d,.]*\s*\w*\s+to\s+\d",
    re.I)
# SCOPE is any concrete anchor that is not itself the result: a named system, a team size, a count.
_SCOPE_EXTRA_RE = re.compile(
    r"\b\d+\s*(?:people|engineers|analysts|clients|customers|reports|sites|teams|states|countries|"
    r"stores|departments|vendors|systems|tables|sources|records|projects|accounts)\b", re.I)
_PROPER_RE = re.compile(r"(?<=[a-z,]\s)(?:[A-Z][a-zA-Z0-9+#.]{2,}|[A-Z]{2,})")


def _has_verb(text):
    """Opens with a real, non-weak action verb."""
    w = rs._first_word(text)
    if not w or w in rs._WEAK_OPENERS or w in _VERB_SWAPS:
        return False
    return not rs._opens_with_nonverb(text)


def _has_result(text):
    stripped = rs._YEARISH_RE.sub(" ", text or "")
    return bool(_RESULT_RE.search(stripped))


def _has_scope(text):
    stripped = rs._YEARISH_RE.sub(" ", text or "")
    if rs._SCOPE_RE.search(stripped) or _SCOPE_EXTRA_RE.search(stripped):
        return True
    if _PROPER_RE.search(text or ""):
        return True              # a named system, tool or team is scope
    return bool(re.search(r"\d", stripped))


def metrics_for(text):
    """The metric types that fit THIS bullet's subject. Generic advice is what makes résumé tools
    useless; a bullet about vendor contracts should be asked for dollars, not for "a number"."""
    for rx, suggestions in _METRIC_HINTS:
        if rx.search(text or ""):
            return list(suggestions)
    return list(_GENERIC_METRICS)


def swaps_for(text):
    """Strong replacements for this bullet's weak opener, or [] if it opens well."""
    return list(_VERB_SWAPS.get(rs._first_word(text), ()))


def analyse_bullet(item):
    """One bullet -> {text, start, end, score, has, missing, problems, swaps, metrics}."""
    text = item["text"]
    has = {"verb": _has_verb(text), "result": _has_result(text), "scope": _has_scope(text)}
    score = round(sum(w for k, w in PART_WEIGHTS.items() if has[k]), 1)

    problems, swaps = [], swaps_for(text)
    if not has["verb"]:
        if swaps:
            problems.append("Opens with a weak verb — it hides what you actually did.")
        else:
            problems.append("Does not open with an action verb.")
    if not has["result"]:
        problems.append("No measurable result — this reads as a duty, not an achievement.")
    if not has["scope"]:
        problems.append("No scope — it does not say how big, how many, or on what.")
    # Length is judged here too, because a bullet can have all three parts and still be unreadable.
    if len(text) > 220:
        problems.append("Over two lines. Split it, or cut the setup and keep the outcome.")
        score = max(0.0, score - 1.0)
    elif len(text) < 40:
        problems.append("Too thin to carry an achievement.")
        score = max(0.0, score - 1.0)

    return {"text": text, "start": item.get("start"), "end": item.get("end"),
            "score": round(min(10.0, score), 1), "has": has,
            "missing": [k for k in ("verb", "scope", "result") if not has[k]],
            "problems": problems, "swaps": swaps,
            "metrics": metrics_for(text) if not has["result"] else []}


def report(text, limit=None):
    """Per-bullet review for a whole résumé, worst first.

    Worst-first because the list is a work queue, not an inventory — the user is going to fix three
    lines and stop, so the three worst have to be the ones at the top.
    """
    _header, sections = rs.split_sections(text or "")
    items, _found = rs._experience_items(sections)
    rows = [analyse_bullet(i) for i in items]
    rows.sort(key=lambda r: (r["score"], r["text"][:40]))
    complete = sum(1 for r in rows if not r["missing"])
    return {
        "bullets": rows[:limit] if limit else rows,
        "total": len(rows),
        "complete": complete,
        # The headline: how many bullets carry all three parts. It is the single number that says
        # whether the experience section is doing its job.
        "complete_pct": int(100.0 * complete / len(rows)) if rows else 0,
        "missing_result": sum(1 for r in rows if "result" in r["missing"]),
        "missing_scope": sum(1 for r in rows if "scope" in r["missing"]),
        "missing_verb": sum(1 for r in rows if "verb" in r["missing"]),
        "find_a_number": list(FIND_A_NUMBER),
    }
