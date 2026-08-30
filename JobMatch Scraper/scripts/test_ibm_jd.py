"""IBM careers JD checks — offline, so CI runs them.

WHAT WENT WRONG, and what is therefore pinned here. IBM's careers search is an Elasticsearch
passthrough that returns exactly the `_source` fields it is asked for. scrape_ibm asks for six,
none of them the description, so every IBM row landed with an empty `jd` — and the fallback,
fetching the posting page, cannot work at all: careers.ibm.com answers a bot challenge (HTTP
202, empty body). 216 live rows sat that way, and scripts/audit_jd_coverage.py classified the
whole host as "gone", because the only thing it probes is the job url.

The live behaviour (a terms query on field_text_01 returns one document per requisition id,
carrying `body` as plain text) was measured against www-api.ibm.com on 2026-08-30: 201 of those
216 ids resolved, median 4,020 chars, in 3.5 s and 0.8 MB. What can silently rot is the SHAPE of
the request — drop `body` from `_source` again, or let `size` default under a 100-id chunk — so
that is what these assert, with the network stubbed.
"""
import inspect
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper
import scraper.score_jobs as sj

fails = []
ran = []


def check(name, got, want):
    ok = got == want
    ran.append(name)
    print("%-4s %-58s %s" % ("ok" if ok else "FAIL", name,
                             "" if ok else "got %r, want %r" % (got, want)))
    if not ok:
        fails.append(name)


def url_for(jid):
    return "https://careers.ibm.com/careers/JobDetail?jobId=%s" % jid


class _Resp(object):
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status
        self.content = json.dumps(payload).encode("utf-8")
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Session(object):
    """Records every POST and answers with the ids the caller asked for.

    `serves` limits which ids exist, so a closed posting can be modelled: IBM drops it from the
    index rather than returning an empty body, and the map must not invent a key for it.
    """

    def __init__(self, serves=None, status=200, text=None):
        self.posts, self.serves, self.status = [], serves, status
        self.text = text if text is not None else "Real IBM description. " * 40

    def post(self, url, json=None, timeout=None, headers=None):
        self.posts.append({"url": url, "body": json or {}, "headers": headers or {}})
        must = ((json or {}).get("query", {}).get("bool", {}).get("must") or [{}])[0]
        ids = (must.get("terms") or {}).get("field_text_01") or []
        if self.serves is not None:
            ids = [i for i in ids if i in self.serves]
        hits = [{"_source": {"_id": "h%s" % i, "url": url_for(i), "field_text_01": i,
                             "body": self.text}} for i in ids]
        return _Resp({"hits": {"hits": hits}}, self.status)


def with_session(sess, fn):
    real = scraper.SESSION
    scraper.SESSION = sess
    try:
        return fn()
    finally:
        scraper.SESSION = real


# --- the requisition id comes out of the stored url ------------------------------------------
def _jid(u):
    m = sj._IBM_JOB_RE.search(u)
    return m.group(1) if m else ""


check("id parsed from the stored JobDetail url", _jid(url_for("106509")), "106509")
check("id parsed regardless of param order",
      _jid("https://careers.ibm.com/careers/JobDetail?lang=en&jobId=99"), "99")
check("no id in a board url", _jid("https://careers.ibm.com"), "")
check("no id in another ATS's url", _jid("https://jobs.lever.co/acme/1"), "")

# --- THE BUG: the request must ask for the description ---------------------------------------
s = _Session()
got = with_session(s, lambda: scraper.ibm_job_bodies(["1", "2"]))
sent = s.posts[0]["body"]
check("one request for a small chunk", len(s.posts), 1)
check("_source asks for `body` — the whole bug", "body" in (sent.get("_source") or []), True)
check("_source asks for the id it keys on",
      "field_text_01" in (sent.get("_source") or []), True)
check("terms query carries exactly the ids asked for",
      sent["query"]["bool"]["must"][0]["terms"]["field_text_01"], ["1", "2"])
check("size covers the chunk, so nothing is truncated away", sent.get("size"), 2)
check("both ids resolved", sorted(got), ["1", "2"])
check("the description is the body text", got["1"].startswith("Real IBM description."), True)

# --- chunking --------------------------------------------------------------------------------
ids = [str(i) for i in range(150)]
s = _Session()
got = with_session(s, lambda: scraper.ibm_job_bodies(ids))
check("150 ids -> 2 requests", len(s.posts), 2)
check("first chunk is IBM_JD_CHUNK ids", s.posts[0]["body"]["size"], 100)
check("second chunk is the remainder", s.posts[1]["body"]["size"], 50)
check("every id resolved across chunks", len(got), 150)

# --- a closed posting is absent, never an empty string ----------------------------------------
s = _Session(serves={"1"})
got = with_session(s, lambda: scraper.ibm_job_bodies(["1", "2"]))
check("an id that left the index yields no key at all", sorted(got), ["1"])

# --- html is cleaned, and a bad answer is not a crash -----------------------------------------
# IBM serves `body` as plain text today, but html_to_text is applied anyway — a feed that starts
# emitting markup must not put tags into the scored text. Asserted on the TAGS, not on the exact
# spacing: get_text(" ") legitimately separates "APIs" from the period it was marked up around.
s = _Session(text="<p>Design <b>APIs</b> for scale.</p>" + "word " * 100)
got = with_session(s, lambda: scraper.ibm_job_bodies(["1"]))
check("html stripped out of the body",
      ("<" not in got["1"], got["1"].startswith("Design APIs")), (True, True))

s = _Session(status=500)
check("a non-200 answers {} rather than raising",
      with_session(s, lambda: scraper.ibm_job_bodies(["1"])), {})
check("no ids -> no request at all",
      with_session(_Session(), lambda: scraper.ibm_job_bodies([])), {})

# --- the map keys by the url it was GIVEN -----------------------------------------------------
# The stored url is whatever canonical_url made of it. Rebuilding one from the feed is how the
# JobDiva miss happened (352 rows fetched, then dropped on a one-character key mismatch), so
# _ibm_jd_map must hand back the caller's own spelling.
stored = "https://careers.ibm.com/careers/JobDetail?jobId=77&extra=1"
s = _Session()
got = with_session(s, lambda: sj._ibm_jd_map({stored, "https://boards.greenhouse.io/x/jobs/9"}))
check("keyed by the caller's url, not a rebuilt one", sorted(got), [stored])
check("non-IBM urls are not queried",
      s.posts[0]["body"]["query"]["bool"]["must"][0]["terms"]["field_text_01"], ["77"])
check("an unset `needed` fetches nothing", sj._ibm_jd_map(None), {})
check("a needed set with no IBM rows fetches nothing",
      sj._ibm_jd_map({"https://jobs.lever.co/acme/1"}), {})

# --- detail_jd routes IBM to the api and never to the page ------------------------------------
# careers.ibm.com serves 202-with-no-body to a page fetch, so a fall-through here is not a
# slower path — it is a permanently empty description. Poison both fallbacks: an ATTEMPT is
# itself the failure.
def _poisoned(*a, **kw):
    raise AssertionError("fell through to a page fetch for an IBM url")


_real = (sj.core.fetch_jd, sj.microdata_jd, scraper.SESSION)
sj.core.fetch_jd, sj.microdata_jd = _poisoned, _poisoned
scraper.SESSION = _Session()
try:
    _u, jd, _d = sj.detail_jd(url_for("106509"))
    check("detail_jd reads an IBM row through the api",
          jd.startswith("Real IBM description."), True)
finally:
    sj.core.fetch_jd, sj.microdata_jd, scraper.SESSION = _real

check("ibm_detail_jd on a non-IBM url is empty",
      with_session(_Session(), lambda: sj.ibm_detail_jd("https://jobs.lever.co/acme/1")), "")

# --- wiring: a bulk family with no branch is a board that silently answers nothing -------------
check("ibm is in the bulk-fetch list", "ibm" in sj.BULK_JD_ATS, True)
src = inspect.getsource(sj.jd_map_for)
check("every bulk family has a jd_map_for branch",
      [a for a in sj.BULK_JD_ATS if ('"%s"' % a) not in src], [])
check("_board_has_missing recognises an IBM backlog",
      sj._board_has_missing("https://careers.ibm.com", "ibm", {url_for("1")}), True)
check("_board_has_missing skips IBM when nothing of its is missing",
      sj._board_has_missing("https://careers.ibm.com", "ibm",
                            {"https://jobs.lever.co/acme/1"}), False)

print("\n%d checks, %d failed." % (len(ran), len(fails)))
if fails:
    print("FAILED: " + ", ".join(fails))
sys.exit(1 if fails else 0)
