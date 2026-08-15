"""Count what a command costs in Supabase egress — the actual bytes, per call site.

WHY THIS EXISTS. The free tier meters egress, and this project has now been surprised by that
number twice: once when a docstring claimed the feed read was "~1 MB" while it had grown to
10.7 MB, and again when a month's quota went in nine days. Both times the fix was easy and
finding it was not, because nothing in the repo could answer "which command spent the bytes?".
`speedtest.py` reports response sizes for the web routes only. This covers ANY entry point —
a test file, a scraper pass, a one-off script — without editing it.

HOW IT ATTACHES. `sitecustomize.py` next door is imported automatically by CPython at startup
for anything on PYTHONPATH, so there is nothing to import in the measured program:

    PYTHONPATH=scripts/egress_probe python -m scraper.score_jobs

It patches `requests.Session.request`, which every db.py call funnels through (`_http.get`,
`.post`, `.head` are all Session methods), rather than patching db itself — db is imported
later than sitecustomize, and this way one hook covers the board scrapers too. Requests to
anything other than the Supabase host are passed through untouched and uncounted.

TWO MODES.
  EGRESS_DRY=1  — nothing leaves the machine. Every Supabase GET answers an empty page and
                  every HEAD answers a corpus count (EGRESS_ROWS, default 20000), so the
                  program's control flow survives and we learn WHICH selects it would issue.
                  Bytes are then modelled from the column list. Costs zero egress, which is
                  the point: you cannot diagnose an overage by re-running the overage.
  (default)     — real requests, real byte counts. `len(r.content)` is the DECODED body, which
                  is what Supabase's meter appears to bill (see the egress notes in db.py):
                  PostgREST honours gzip and `requests` asks for it, yet the daily totals only
                  reconcile against uncompressed figures.

The dry model is calibrated against the live measurements in this repo: 6,664 B/row for
`select=*` (the jd column is ~6 KB of it), 601 B/row for the feed columns, and 175 B/row for
url alone — the url is 29% of a feed row and is why column-narrowing has a floor.
"""
import json
import os
import sys
import time
import atexit
import collections

_SUPABASE_HOST = ".supabase.co"

# Modelled cost of one row, by what was selected. Derived from the live measurements above:
# url is a fixed 175 B, every other scalar column averages ~22 B, and jd is the rest.
_B_URL, _B_COL, _B_JD = 175, 22, 6000
_PAGE = 1000                      # PostgREST's per-request row cap; db._fetch_all pages on it

_calls = collections.defaultdict(lambda: {"n": 0, "bytes": 0, "rows": 0, "secs": 0.0})
_installed = False


def _rows_hint():
    try:
        return int(os.environ.get("EGRESS_ROWS") or 20000)
    except ValueError:
        return 20000


def _keyed_rows(params):
    """Rows an exact-key filter can return, or None if no filter in `params` bounds it.

    PostgREST spells these `col=eq.value` and `col=in.("a","b")`; db.py builds the second in
    `_in_list`. Only primary-key columns bound a count — a non-unique `company=eq.X` could
    match thousands — so this looks at `url` alone, which is the jobs table's key and the only
    column db.py filters a bulk read on.
    """
    v = str(params.get("url") or "")
    if v.startswith("eq."):
        return 1
    if v.startswith("in.("):
        return max(1, v.count(",") + 1)         # operand count; urls carry no bare commas
    return None


def _model_bytes(method, table, select, params):
    """Estimated decoded bytes for `select` — dry mode only.

    A HEAD carries no body at all; that is the whole point of `table_count()`, so it must model
    as free or the cheap probe looks expensive and gets "optimised" away.

    THE PAGING EXTRAPOLATION IS THE SUBTLE PART. `db._fetch_all` walks a table 1,000 rows at a
    time and stops when a short page comes back — but dry mode answers every page empty, so the
    walk stops after ONE request and would be modelled at a twentieth of its real cost. A
    request carrying `offset` IS such a walk, so page one is charged for the whole corpus.

    A KEY FILTER MUST BE HONOURED, or the pessimism stops being conservative and starts being
    wrong. `load_jobs_by_urls` asks for `select=*` — the jd column, ~6 KB a row — but only for
    a named list of URLs, and it pages, so the two rules above together charged it for the
    entire corpus with descriptions: 130 MB for what is really a few hundred rows. That is a
    100x error in the one direction that matters, since it invents a culprit. `url=in.(...)`
    and `url=eq....` bound the answer exactly, so use that bound. Filters that cannot bound a
    row count (`jd=not.is.null`, `or=(...)`) stay pessimistic.
    """
    if method == "HEAD":
        return 0, 0
    limit, walking = params.get("limit"), "offset" in params
    keyed = _keyed_rows(params)
    if keyed is not None:
        rows = keyed
    elif walking:
        rows = _rows_hint()
    elif str(limit).isdigit():
        rows = min(int(limit), _rows_hint())
    else:
        rows = min(_PAGE, _rows_hint())
    if table != "jobs":
        rows = min(rows, 2000)            # user/board/audit tables are small; don't model them big
    sel = (select or "*").strip()
    if sel == "*":
        per = _B_URL + _B_JD + 30 * _B_COL
    else:
        cols = [c for c in sel.split(",") if c.strip()]
        per = (_B_URL if any(c.strip() == "url" for c in cols) else 0) \
            + _B_COL * len([c for c in cols if c.strip() != "url"]) \
            + (_B_JD if any(c.strip() == "jd" for c in cols) else 0)
    return rows * per, rows


def _key(method, url, params):
    """Group calls by what they ask for, not by which line asked — one line in a loop and a
    hundred lines asking the same thing cost the same and want the same fix."""
    table = url.rsplit("/rest/v1/", 1)[-1].split("?")[0] or "?"
    if method == "GET" and "rpc/" in url:
        table = "rpc:" + table
    sel = (params or {}).get("select") or "*"
    if isinstance(sel, (list, tuple)):
        sel = ",".join(sel)
    sel = str(sel)
    # ASCII only from here down: this prints to a Windows console under cp1252, where a stray
    # em-dash or ellipsis comes out as a replacement character and makes the report look broken.
    return method, table, (sel[:70] + "..." if len(sel) > 70 else sel)


def install():
    global _installed
    if _installed:
        return
    try:
        import requests
        from requests.models import Response
    except Exception:
        return                            # no requests -> nothing talks to Supabase anyway
    _installed = True
    dry = (os.environ.get("EGRESS_DRY") or "").lower() in ("1", "true", "yes")
    real_request = requests.Session.request

    def _fake(method, params):
        """A response shaped enough that db.py's callers behave normally without a network."""
        r = Response()
        r.status_code = 200
        r.url = ""
        if method == "HEAD":
            # table_count() parses the tail of Content-Range; give it a real-looking corpus so
            # jobs_fingerprint() reports a count and callers take their normal branch.
            r.headers["Content-Range"] = "0-999/%d" % _rows_hint()
            r._content = b""
        else:
            r._content = b"[]"            # an empty page: _fetch_all stops, callers see no rows
            r.headers["Content-Type"] = "application/json"
        return r

    def patched(self, method, url, **kw):
        if _SUPABASE_HOST not in str(url):
            return real_request(self, method, url, **kw)
        params = kw.get("params") or {}
        k = _key(str(method).upper(), str(url), params)
        rec = _calls[k]
        rec["n"] += 1
        t0 = time.time()
        if dry:
            resp = _fake(str(method).upper(), params)
            b, rows = _model_bytes(k[0], k[1], params.get("select"), params)
            rec["bytes"] += b
            rec["rows"] += rows
        else:
            resp = real_request(self, method, url, **kw)
            try:
                rec["bytes"] += len(resp.content or b"")
                body = resp.json() if resp.content else None
                rec["rows"] += len(body) if isinstance(body, list) else 0
            except Exception:
                pass
        rec["secs"] += time.time() - t0
        return resp

    requests.Session.request = patched
    atexit.register(report)


def _mb(n):
    return n / 1048576.0


def report():
    if not _calls:
        return
    dry = (os.environ.get("EGRESS_DRY") or "").lower() in ("1", "true", "yes")
    rows = sorted(_calls.items(), key=lambda kv: -kv[1]["bytes"])
    total = sum(v["bytes"] for _, v in rows)
    n = sum(v["n"] for _, v in rows)
    label = os.environ.get("EGRESS_LABEL") or " ".join(sys.argv[:2])
    out = sys.stderr
    print("\n" + "=" * 78, file=out)
    print("EGRESS  %s%s" % (label, "   [DRY - modelled, nothing sent]" if dry else ""), file=out)
    print("  %8.2f MB over %d request(s)" % (_mb(total), n), file=out)
    print("=" * 78, file=out)
    print("  %9s %6s  %-6s %-14s %s" % ("MB", "calls", "method", "table", "select"), file=out)
    for (method, table, sel), v in rows[:25]:
        print("  %9.3f %6d  %-6s %-14s %s" % (_mb(v["bytes"]), v["n"], method, table, sel),
              file=out)
    print("=" * 78 + "\n", file=out)

    path = os.environ.get("EGRESS_REPORT")
    if path:
        # Appended as JSON lines so a whole suite can be run one file per line and summed after,
        # which is the only way to see that no single command looks expensive but forty do.
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "label": label, "dry": dry, "bytes": total, "calls": n,
                    "by_call": [{"method": m, "table": t, "select": s, **v}
                                for (m, t, s), v in rows],
                }) + "\n")
        except Exception:
            pass
