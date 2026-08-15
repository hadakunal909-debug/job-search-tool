#!/usr/bin/env python3
"""dbproxy's authentication and its refusals.

This endpoint is the one piece of the migration that is reachable from the public internet, so
the assertions that matter are the negative ones: what it must NOT do when the caller is lying.
A wrong answer here is not a broken feature, it is somebody else's writes landing in the
database — so every check below is a way the endpoint could be attacked or misused, and the
round-trip test at the end exists only to prove the refusals aren't refusing everything.

No Flask and no database: dbproxy.handle takes the backend as an argument precisely so this
can run anywhere, including CI, where there are no credentials.

    python scripts/test_dbproxy.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import dbproxy
import pgrest

SECRET = "test-secret-value"
fails, ran = [], []


def check(label, ok, got=None):
    ran.append(label)
    print("  %s  %s" % ("ok " if ok else "FAIL", label))
    if not ok:
        fails.append(label)
        if got is not None:
            print("        got: %s" % (got,))


class FakeBackend:
    """Records what reached it. Anything appearing here got past every guard."""

    def __init__(self):
        self.calls = []

    def _mk(self, method):
        def f(url, headers=None, params=None, data=None, timeout=None):
            self.calls.append((method, url, params, (headers or {}).get("Prefer"), data))
            if method == "HEAD":
                return pgrest.Response(200, None, {"Content-Range": "0-9/10"})
            return pgrest.Response(200, [{"url": "u1", "title": "T"}])
        return f

    def __getattr__(self, name):
        if name in ("get", "post", "patch", "delete", "head"):
            return self._mk(name.upper())
        raise AttributeError(name)


def envelope(**kw):
    e = {"method": "GET", "table": "jobs", "params": {"select": "url"}, "prefer": "", "body": None}
    e.update(kw)
    return json.dumps(e, separators=(",", ":")).encode("utf-8")


def call(raw, secret=SECRET, ts=None, sig=None, backend=None, now=None):
    ts = ts if ts is not None else "%d" % int(time.time())
    sig = sig if sig is not None else dbproxy.sign(SECRET, ts, raw)
    return dbproxy.handle(raw, ts, sig, secret, backend or FakeBackend(), now)


print("=" * 74)
print("AUTHENTICATION")
print("=" * 74)
raw = envelope()
st, _ = call(raw)
check("a correctly signed request is accepted", st == 200, st)

st, body = call(raw, sig="0" * 64)
check("a wrong signature is rejected", st == 401 and "signature" in body["error"], (st, body))

st, body = call(raw, sig=None if False else dbproxy.sign("some-other-secret", "%d" % int(time.time()), raw))
check("a signature from a DIFFERENT secret is rejected", st == 401, (st, body))

tampered = envelope(table="users")
st, body = call(tampered, sig=dbproxy.sign(SECRET, "%d" % int(time.time()), raw))
check("a body edited after signing is rejected", st == 401, (st, body))

old = "%d" % (int(time.time()) - dbproxy.MAX_SKEW - 60)
st, body = call(raw, ts=old, sig=dbproxy.sign(SECRET, old, raw))
check("a correctly signed but STALE request is rejected (replay window)",
      st == 401 and "stale" in body["error"], (st, body))

st, body = call(raw, ts="not-a-number", sig=dbproxy.sign(SECRET, "not-a-number", raw))
check("a non-numeric timestamp is rejected", st == 401, (st, body))

st, body = call(raw, sig="")
check("a missing signature is rejected", st == 401, (st, body))

st, body = dbproxy.handle(raw, "1", "x", "", FakeBackend())
check("with no secret configured the endpoint is OFF, not merely unauthenticated",
      st == 503, (st, body))

# The signature must gate everything: a request that would be refused on its content must still
# be refused as UNAUTHENTICATED when unsigned, never with a hint about why the content was bad.
st, body = call(envelope(table="pg_shadow"), sig="0" * 64)
check("an unsigned request is refused before its content is judged",
      st == 401 and "table" not in body["error"], (st, body))

st, body = dbproxy.handle(raw, "1", "bad-sig", SECRET, FakeBackend(), local_ok=False)
check("an unsigned caller cannot learn whether the proxy has a database",
      st == 401 and "PG_DSN" not in body["error"], (st, body))

st, body = call(raw, backend=FakeBackend(), now=None)
check("...while a SIGNED caller gets the real 503 when it doesn't",
      dbproxy.handle(raw, "%d" % int(time.time()),
                     dbproxy.sign(SECRET, "%d" % int(time.time()), raw),
                     SECRET, FakeBackend(), local_ok=False)[0] == 503, st)

print()
print("=" * 74)
print("WHAT IT REFUSES TO PROXY")
print("=" * 74)
for tbl in ("pg_shadow", "pg_catalog.pg_user", "information_schema.tables", "other_tenant"):
    st, body = call(envelope(table=tbl))
    check("table not on the allowlist: %s" % tbl, st == 403, (st, body))

st, body = call(envelope(table="rpc/db_stats"))
check("an allowlisted rpc is permitted", st == 200, (st, body))
st, body = call(envelope(table="rpc/pg_sleep"))
check("an rpc that is not allowlisted is refused", st == 403, (st, body))

st, body = call(envelope(method="TRACE"))
check("an unsupported method is refused", st == 400, (st, body))

st, body = call(envelope(method="DELETE", params={}))
check("an UNFILTERED delete is refused by the translator, not proxied",
      st == 400 and "refused" in body["error"], (st, body))

st, body = call(envelope(params={"select": "url; drop table jobs"}))
check("an identifier that isn't one is refused", st == 400, (st, body))

st, body = call(envelope(params={"title": "cs.{x}"}))
check("an operator outside the supported subset is refused", st == 400, (st, body))

st, body = call(b"{not json")
check("a malformed envelope is refused", st == 400, (st, body))

st, body = call(envelope(params=["not", "an", "object"]))
check("params that are not an object are refused", st == 400, (st, body))

print()
print("=" * 74)
print("WHAT IT DOES PROXY — the refusals above must not be refusing everything")
print("=" * 74)
b = FakeBackend()
st, body = call(envelope(params={"select": "url,title", "order": "url", "limit": 1000}), backend=b)
check("a select reaches the backend", st == 200 and len(b.calls) == 1, (st, b.calls))
check("...with its params intact",
      b.calls[0][2] == {"select": "url,title", "order": "url", "limit": 1000}, b.calls[0][2])
check("...and the rows come back", body["rows"] == [{"url": "u1", "title": "T"}], body)

b = FakeBackend()
st, body = call(envelope(method="POST", table="jobs", params={"on_conflict": "url"},
                         prefer="resolution=merge-duplicates,return=minimal",
                         body=[{"url": "u1", "title": "T"}]), backend=b)
check("an upsert reaches the backend with its Prefer header",
      st == 200 and b.calls[0][3] == "resolution=merge-duplicates,return=minimal", b.calls)
check("...and its body survives the round trip",
      json.loads(b.calls[0][4]) == [{"url": "u1", "title": "T"}], b.calls[0][4])

b = FakeBackend()
st, body = call(envelope(method="HEAD", prefer="count=exact"), backend=b)
check("a HEAD returns Content-Range, which is how table_count works",
      body["headers"]["Content-Range"] == "0-9/10", body)

print()
print("=" * 74)
print("CLIENT/SERVER AGREEMENT — the two halves must sign the same bytes")
print("=" * 74)
sent = {}


class CaptureHTTP:
    def post(self, url, data=None, timeout=None, headers=None):
        sent.update(raw=data, ts=headers.get("X-DB-Ts"), sig=headers.get("X-DB-Sig"))
        return pgrest.Response(200, {"status": 200, "rows": [], "headers": {}})


s = dbproxy.Session("https://example.test/api/db", SECRET)
s._http = CaptureHTTP()
s.get("https://x/rest/v1/jobs", headers={}, params={"select": "url"})
ok, why = dbproxy.verify(SECRET, sent["ts"], sent["raw"], sent["sig"])
check("what the client signs is what the server verifies", ok, why)
st, _ = dbproxy.handle(sent["raw"], sent["ts"], sent["sig"], SECRET, FakeBackend())
check("...and a real client request is accepted end to end", st == 200, st)

print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all good - %d checks" % len(ran))
