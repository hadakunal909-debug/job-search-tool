"""
scraper/liveness.py — is this posting dead, or does it just not want to talk to us?

Split out of scripts/close_dead_jds.py._probe so the decision is a PURE function of what came
back: (status, body, request_url, final_url) -> verdict. That matters because the expensive
mistake here is one-directional. Marking a dead posting live costs a wasted click. Marking a
LIVE posting closed removes a job the user could have applied to and we never find out. So every
rule below is written to fail towards "uncertain".

The old classifier, inline in _probe, had three holes:

  * It never looked at the BODY. A Cloudflare interstitial answers 403 (caught) or 200 with a
    "Just a moment…" page (not caught) — and the latter is indistinguishable from a live posting
    by status alone.
  * 429 and 503 fell through to "unknown" instead of "blocked". Rate-limited is the one state
    most likely to hit a whole host at once, which is exactly when a wrong verdict is worst.
  * A redirect that dropped the job id — the shape of "this posting is gone, here is our jobs
    page" but ALSO the shape of "we moved to a new URL scheme" — got whatever the landing page
    happened to return.

Guard order is deliberate and matches santifer/career-ops `liveness-core.mjs` (MIT), which is
where the phrase list and the redirect-lost-the-id rule come from: check the reasons NOT to
believe a response before drawing any conclusion from it. Only 404 and 410 are hard-dead.

Verdicts:
  gone       — the posting is withdrawn. The ONLY verdict scripts/close_dead_jds.py acts on.
  blocked    — the host refuses server-side reads (bot wall, 401/403/405/429/503).
  transient  — a server error that says nothing about the posting. Retry later.
  uncertain  — we reached something, but not provably this posting.
  unknown    — 200 with real content and still no description. A parser problem, not a dead job.
"""

import re

# Body threshold for "a 200 that is not a page". Deliberately the same order of magnitude as
# core._MIN_JD_CHARS: below this there is no posting here, whatever the status line said.
MIN_BODY_CHARS = 500

# Anti-bot interstitials. Every one of these is a page that returned successfully and contains no
# posting, so status alone cannot tell it apart from a real one. Lower-cased substring match: these
# are boilerplate strings from a handful of vendors, not user content, so precision is not at risk.
BOT_WALL_PHRASES = (
    "just a moment",                     # Cloudflare
    "checking your browser",             # Cloudflare (older)
    "performing security verification",  # Cloudflare Turnstile
    "enable javascript and cookies to continue",
    "ddos protection by",
    "attention required!",
    "access denied",
    "request unsuccessful. incapsula",   # Imperva
    "pardon our interruption",           # Distil / Imperva
    "are you a robot",
    "verify you are human",
    "human verification",                # iCIMS, served with a 405 of all things
    "px-captcha",                        # PerimeterX
    "unusual traffic from your",
    "bot detection",
)

# Pages that say, in words, that the posting is over. Multi-language because a US-listed employer
# routinely serves a localised careers site. Kept short: a phrase that also appears on a LIVE
# posting would close live jobs, so "filled" alone is not here -- "the position has been filled"
# is, because an application form does not say that.
GONE_PHRASES = (
    "no longer available",
    "no longer accepting applications",
    "no longer accepting online applications",
    "this job is closed",
    "this position has been filled",
    "the position has been filled",
    "posting has expired",
    "job posting has expired",
    "this vacancy has expired",
    "offre expirée", "n'est plus disponible", "offre pourvue",
    "stelle ist nicht mehr",
    "esta oferta ya no está disponible",
)

# What a job id looks like in a URL: a UUID, or a run of 5+ digits. Fewer digits than that and a
# page number or a year would qualify, and every redirect would read as a lost id.
_JOB_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9]{5,}", re.I)

_HARD_DEAD = ("404", "410")
_REFUSED = ("401", "403", "405", "407", "429", "451", "503")


def bot_walled(body):
    """The phrase that proves this is an interstitial, or "" — returned rather than a bool so the
    caller can log WHICH wall it hit. Cheap enough to run on every probe."""
    low = (body or "")[:20000].lower()      # walls are always at the top; do not scan a 2MB page
    for p in BOT_WALL_PHRASES:
        if p in low:
            return p
    return ""


def says_gone(body):
    """The phrase in which the page states the posting is over, or ""."""
    low = (body or "")[:40000].lower()
    for p in GONE_PHRASES:
        if p in low:
            return p
    return ""


def job_ids(url):
    return set(m.group(0).lower() for m in _JOB_ID_RE.finditer(url or ""))


def lost_the_job_id(request_url, final_url):
    """True when the URL we asked for carried a job id and the URL we ended on does not.

    That is the signature of landing on a listing page. It is NOT evidence the posting is gone:
    plenty of boards renumber, and some redirect through a locale picker. So it downgrades a
    conclusion to `uncertain` and never produces one.
    """
    if not request_url or not final_url or request_url == final_url:
        return False
    ids = job_ids(request_url)
    return bool(ids) and not (ids & job_ids(final_url))


def classify(status, body="", request_url="", final_url=""):
    """(verdict, why). `status` is the HTTP code as a STRING, or "ERR:<ExceptionName>".

    Order is the whole design: reasons to distrust the response come first, so a bot wall can
    never be read as a withdrawn posting.
    """
    status = str(status or "")

    # 1. Guards. A response we cannot trust yields no conclusion, whatever its status line.
    wall = bot_walled(body)
    if wall:
        return "blocked", "anti-bot interstitial (%r)" % wall
    if status in _REFUSED:
        return "blocked", "HTTP %s — refuses server-side reads" % status
    if status.startswith("ERR"):
        return "blocked", status
    if status.startswith("5"):
        return "transient", "HTTP %s — server error, says nothing about the posting" % status

    # 2. Hard evidence. Only these two codes mean the posting itself is gone.
    if status in _HARD_DEAD:
        return "gone", "HTTP %s" % status

    # 3. The page saying so in words. Only trusted on a 2xx: a phrase found inside an error
    #    template is the error template's text, not the posting's.
    if status.startswith("2"):
        phrase = says_gone(body)
        if phrase:
            return "gone", "page says %r" % phrase

    # 4. Did we even reach this posting?
    if lost_the_job_id(request_url, final_url):
        return "uncertain", "redirected to a URL without the job id — probably a listing page"

    # 5. A 200 that is not a page. Reached only when no wall matched, which is the point: this
    #    rule used to fire on interstitials and empty JS shells alike.
    if status.startswith("2") and len(body or "") < MIN_BODY_CHARS:
        return "gone", "HTTP %s with a %d-byte body" % (status, len(body or ""))

    if not status:
        return "uncertain", "no response recorded"
    return "unknown", "HTTP %s with content but no extractable description" % status


# How each verdict is allowed to be used. Imported rather than re-derived by callers so a new
# verdict cannot silently start closing jobs.
CLOSES_THE_POSTING = frozenset(("gone",))

VERDICT_NOTES = {
    "readable": "an extractor works here — do not close",
    "gone": "posting withdrawn",
    "blocked": "refuses server-side reads",
    "transient": "server erroring — retry, do not conclude",
    "uncertain": "could not confirm we reached the posting",
    "unknown": "200 with content and still no text",
}
