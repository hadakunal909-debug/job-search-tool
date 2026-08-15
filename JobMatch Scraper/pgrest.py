"""pgrest.py — speak PostgREST's dialect straight to a Postgres server.

WHY. db.py talks to Supabase over PostgREST: 47 call sites across 75 public functions, using
PostgREST's own query language (`in.(...)`, `not.is.null`, `gte.`, `or=(...)`), its upsert header
(`Prefer: resolution=merge-duplicates` plus `on_conflict=`), and its row-count trick (a HEAD whose
`Content-Range` carries the total). Move the database to a plain Postgres — cPanel, a VPS, Neon,
anything — and none of that exists.

The obvious response is to rewrite those 75 functions in psycopg. This is the cheaper one: every
call already funnels through `_http.get/post/patch/delete/head`, so an object that implements
those five methods and translates the requests into SQL drops in underneath and NOTHING above it
changes. web.py, the scraper, the admin panels and all 21 test suites keep running against the
same db.py, and `PG_DSN` is the only switch. Unset it and you are back on Supabase — which is
what makes this safe to try, and reversible if the host disappoints.

SCOPE IS DELIBERATELY THE SUBSET db.py ACTUALLY EMITS, no more: it was read out of the source,
not guessed from PostgREST's manual. Anything outside it raises loudly rather than guessing,
because a filter this layer silently mistranslates is a wrong answer, not an error — the exact
failure mode that makes storage bugs expensive.

  verbs      GET, POST, PATCH, DELETE, HEAD
  select     column lists and `*`
  filters    eq. neq. gt. gte. lt. lte. like. ilike. is.null not.is.null in.(...)
  or=        `(jd.is.null,jd.eq.)` — one flat OR group, which is all db.py uses
  modifiers  order (with .desc / .nullslast), limit, offset
  headers    Prefer: resolution=merge-duplicates | return=minimal | count=exact
  rpc        POST /rpc/<fn> -> select * from <fn>()

DATES ARE THE SUBTLE PART. PostgREST hands back ISO STRINGS; psycopg hands back date/datetime
objects. Half this codebase compares those values as strings (db.row_age_date, the freshness
filters, jobs_fingerprint's max first_seen), so returning native objects would not crash — it
would silently change comparison semantics. Everything is normalised to the JSON shapes
PostgREST produces before it leaves here.
"""
import datetime
import decimal
import json
import re
import uuid

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OPS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
        "like": "LIKE", "ilike": "ILIKE"}
# Query-string keys that are modifiers rather than column filters.
_RESERVED = {"select", "order", "limit", "offset", "on_conflict", "columns"}


class PgRestError(Exception):
    """A request this shim will not translate. Raised rather than approximated — see the scope
    note above."""


class JsonValue:
    """A body value bound for a jsonb column.

    psycopg cannot bind a bare dict — `can't adapt type 'dict'` — and this codebase has seven
    jsonb columns (profiles.search_prefs, admin_audit.detail, events.props, scrape_status.data,
    learned_answers.options, tailored_cache.data, profiles.extra). PostgREST hands them back as
    dicts and lists, so they arrive here needing a wrapper.

    A marker rather than a global `register_adapter(dict, Json)` because a global adapter cannot
    tell the two meanings of a Python LIST apart: a list in a request body is jsonb, but a list
    in a WHERE argument is the operand of `in.(...)`, which must bind as a Postgres ARRAY for
    `= ANY(%s)`. Adapting those to jsonb would break every load_jobs_by_urls call. Only body
    values get wrapped, at the point where we still know which is which.
    """
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __eq__(self, other):                    # so the translation tests can assert on args
        return self.value == (other.value if isinstance(other, JsonValue) else other)

    def __repr__(self):
        return "JsonValue(%r)" % (self.value,)


def bind(v):
    """Body value -> what should be bound for it."""
    return JsonValue(v) if isinstance(v, (dict, list)) else v


def ident(name):
    """A validated SQL identifier. Every table and column here comes from db.py's own constants,
    never from a user, so this is a tripwire for a typo or a future caller doing something
    dynamic — not a defence against injection from the feed."""
    name = (name or "").strip()
    if not _IDENT.match(name):
        raise PgRestError("refusing unsafe identifier: %r" % name)
    return '"%s"' % name


def jsonify(v):
    """The value PostgREST would have put in its JSON body.

    date/datetime -> ISO string, Decimal -> float, UUID/memoryview -> str. jsonb columns already
    arrive as dict/list from psycopg and pass through, which matches PostgREST exactly.
    """
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode("utf-8", "replace")
    if isinstance(v, dict):
        return {k: jsonify(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonify(x) for x in v]
    return v


def parse_in_list(operand):
    """`("a","b\\"c")` -> ['a', 'b"c'] — the inverse of db._in_list.

    Hand-parsed rather than split on commas: db._in_list double-quotes every value and escapes
    embedded quotes and backslashes precisely so a URL containing a comma cannot break out of
    the list, and a naive split would put it back in.
    """
    s = operand.strip()
    if not (s.startswith("(") and s.endswith(")")):
        raise PgRestError("malformed in.() operand: %r" % operand)
    s, out, i = s[1:-1], [], 0
    while i < len(s):
        if s[i] == ",":
            i += 1
            continue
        if s[i] == '"':
            buf, i = [], i + 1
            while i < len(s):
                if s[i] == "\\" and i + 1 < len(s):
                    buf.append(s[i + 1]); i += 2; continue
                if s[i] == '"':
                    i += 1; break
                buf.append(s[i]); i += 1
            out.append("".join(buf))
        else:                                   # bare, unquoted operand
            j = s.find(",", i)
            j = len(s) if j < 0 else j
            out.append(s[i:j]); i = j
    return out


def condition(col, spec):
    """One PostgREST filter -> ('sql', [args]).

    `spec` is `<op>.<operand>`, split on the FIRST dot only — the operand is very often a URL and
    is full of them. `not.` is the one prefix db.py uses (`not.is.null`).
    """
    negate = False
    if spec.startswith("not."):
        negate, spec = True, spec[4:]
    op, _, operand = spec.partition(".")
    if op == "is":
        if operand.lower() != "null":
            raise PgRestError("only is.null is supported, got is.%s" % operand)
        sql = "%s IS %sNULL" % (ident(col), "NOT " if negate else "")
        return sql, []
    if op == "in":
        vals = parse_in_list(operand)
        if not vals:                            # `in.()` matches nothing; ANY(empty) would too,
            return ("TRUE" if negate else "FALSE"), []      # but be explicit about it
        # `<> ALL` is the correct negation of `= ANY` — NOT (x = ANY(l)) is x <> ALL(l).
        sql = ("%s <> ALL(%%s)" % ident(col)) if negate else ("%s = ANY(%%s)" % ident(col))
        return sql, [vals]
    if op in _OPS:
        # PostgREST spells LIKE wildcards with `*`.
        if op in ("like", "ilike"):
            operand = operand.replace("*", "%")
        sql = "%s %s %%s" % (ident(col), _OPS[op])
        return (("NOT (%s)" % sql) if negate else sql), [operand]
    raise PgRestError("unsupported operator %r on column %r" % (op, col))


def where(params):
    """The WHERE clause for every non-reserved query param. Returns ('', []) when unfiltered."""
    parts, args = [], []
    for k, v in (params or {}).items():
        if k in _RESERVED:
            continue
        v = "" if v is None else str(v)
        if k == "or":
            # `or=(jd.is.null,jd.eq.)` — one flat group of column.op.operand terms. Nested groups
            # are legal PostgREST and are NOT supported here; db.py emits exactly this shape.
            inner = v.strip()
            if not (inner.startswith("(") and inner.endswith(")")):
                raise PgRestError("malformed or= group: %r" % v)
            ors, oargs = [], []
            for term in _split_top(inner[1:-1]):
                col, _, spec = term.partition(".")
                s, a = condition(col, spec)
                ors.append(s); oargs.extend(a)
            if ors:
                parts.append("(%s)" % " OR ".join(ors))
                args.extend(oargs)
            continue
        s, a = condition(k, v)
        parts.append(s); args.extend(a)
    return (" WHERE " + " AND ".join(parts)) if parts else "", args


def _split_top(s):
    """Split on commas that are not inside parens or quotes — `or=` terms can carry an in.()."""
    out, depth, quoted, buf = [], 0, False, []
    i = 0
    while i < len(s):
        c = s[i]
        if quoted:
            buf.append(c)
            if c == "\\" and i + 1 < len(s):
                buf.append(s[i + 1]); i += 2; continue
            if c == '"':
                quoted = False
        elif c == '"':
            quoted = True; buf.append(c)
        elif c == "(":
            depth += 1; buf.append(c)
        elif c == ")":
            depth -= 1; buf.append(c)
        elif c == "," and depth == 0:
            out.append("".join(buf).strip()); buf = []
        else:
            buf.append(c)
        i += 1
    if buf:
        out.append("".join(buf).strip())
    return [x for x in out if x]


def select_list(sel):
    if not sel or sel == "*":
        return "*"
    return ", ".join(ident(c.strip()) for c in str(sel).split(",") if c.strip())


def order_by(spec):
    """`first_seen.desc.nullslast` / `at.desc` / `url` -> SQL. PostgREST's default is NULLS
    LAST for ascending and NULLS FIRST for descending, same as Postgres, so only an explicit
    nullslast/nullsfirst is emitted."""
    if not spec:
        return ""
    outs = []
    for part in str(spec).split(","):
        bits = [b for b in part.strip().split(".") if b]
        if not bits:
            continue
        col, mods = bits[0], [b.lower() for b in bits[1:]]
        sql = ident(col)
        if "desc" in mods:
            sql += " DESC"
        elif "asc" in mods:
            sql += " ASC"
        if "nullslast" in mods:
            sql += " NULLS LAST"
        elif "nullsfirst" in mods:
            sql += " NULLS FIRST"
        outs.append(sql)
    return (" ORDER BY " + ", ".join(outs)) if outs else ""


def limit_offset(params):
    sql, args = "", []
    if str(params.get("limit", "")).strip().isdigit():
        sql += " LIMIT %s"; args.append(int(params["limit"]))
    if str(params.get("offset", "")).strip().isdigit():
        sql += " OFFSET %s"; args.append(int(params["offset"]))
    return sql, args


def build(method, table, params, body, prefer):
    """(sql, args, wants_rows) for one translated request. Pure — no database, no connection —
    so scripts/test_pgrest.py can assert every statement this layer will ever produce without a
    server anywhere near it."""
    params = params or {}
    minimal = "return=minimal" in prefer
    w, wargs = where(params)

    if method in ("GET", "HEAD"):
        if method == "HEAD":
            return "SELECT count(*) AS n FROM %s%s" % (ident(table), w), wargs, True
        lo, largs = limit_offset(params)
        sql = "SELECT %s FROM %s%s%s%s" % (select_list(params.get("select")), ident(table),
                                           w, order_by(params.get("order")), lo)
        return sql, wargs + largs, True

    if method == "POST":
        rows = body if isinstance(body, list) else [body]
        if not rows:
            raise PgRestError("POST with no rows")
        cols = list(rows[0].keys())
        # PostgREST requires every object in a bulk write to share the same keys, and db._upsert
        # normalises to the union before sending. Enforcing it here turns a caller that forgets
        # into an error instead of a row with silently missing columns.
        for r in rows:
            if list(r.keys()) != cols:
                raise PgRestError("bulk insert rows must share identical keys")
        collist = ", ".join(ident(c) for c in cols)
        ph = ", ".join("(%s)" % ", ".join(["%s"] * len(cols)) for _ in rows)
        args = [bind(r[c]) for r in rows for c in cols]
        sql = "INSERT INTO %s (%s) VALUES %s" % (ident(table), collist, ph)
        conflict = params.get("on_conflict")
        if "resolution=merge-duplicates" in prefer:
            if not conflict:
                raise PgRestError("merge-duplicates without on_conflict=")
            keys = [c.strip() for c in str(conflict).split(",") if c.strip()]
            sets = [c for c in cols if c not in keys]
            sql += " ON CONFLICT (%s) DO %s" % (
                ", ".join(ident(k) for k in keys),
                ("UPDATE SET " + ", ".join("%s = EXCLUDED.%s" % (ident(c), ident(c))
                                           for c in sets)) if sets else "NOTHING")
        elif "resolution=ignore-duplicates" in prefer:
            sql += " ON CONFLICT DO NOTHING"
        if not minimal:
            sql += " RETURNING *"
        return sql, args, not minimal

    if method == "PATCH":
        if not isinstance(body, dict) or not body:
            raise PgRestError("PATCH needs a single object body")
        cols = list(body.keys())
        sql = "UPDATE %s SET %s%s" % (ident(table),
                                      ", ".join("%s = %%s" % ident(c) for c in cols), w)
        args = [bind(body[c]) for c in cols] + wargs
        if not minimal:
            sql += " RETURNING *"
        return sql, args, not minimal

    if method == "DELETE":
        # An unfiltered DELETE is almost certainly a bug in the caller, and it would empty a
        # table. db.delete_all exists and goes through a filter; nothing legitimately gets here
        # without a WHERE.
        if not w:
            raise PgRestError("refusing an unfiltered DELETE on %s" % table)
        sql = "DELETE FROM %s%s" % (ident(table), w)
        if not minimal:
            sql += " RETURNING *"
        return sql, wargs, not minimal

    raise PgRestError("unsupported method %s" % method)


class Response:
    """Duck-types the parts of requests.Response that db.py reads."""

    def __init__(self, status_code=200, rows=None, headers=None):
        self.status_code = status_code
        self._rows = rows
        self.headers = headers or {}

    def json(self):
        return self._rows

    @property
    def text(self):
        try:
            return json.dumps(self._rows)
        except Exception:
            return str(self._rows)

    @property
    def content(self):
        return self.text.encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise PgRestError("HTTP %s: %s" % (self.status_code, self.text[:300]))


class Session:
    """The `_http` replacement. One lazily-opened connection, autocommit, dict rows."""

    def __init__(self, dsn):
        self.dsn = dsn
        self._conn = None
        self._v3 = None

    def _connect(self):
        if self._conn is not None:
            return self._conn
        try:
            import psycopg                              # psycopg 3
            self._conn = psycopg.connect(self.dsn, autocommit=True)
            self._v3 = True
        except ImportError:
            import psycopg2                             # psycopg 2, what cPanel usually has
            import psycopg2.extras                      # noqa: F401  (registers the dict cursor)
            self._conn = psycopg2.connect(self.dsn)
            self._conn.autocommit = True
            self._v3 = False
        return self._conn

    def _json(self, marked):
        """JsonValue -> the driver's jsonb wrapper. Both drivers need one; neither can bind a
        bare dict, and psycopg2's error for that ("can't adapt type 'dict'") names the Python
        type rather than the column, so it is worth knowing this is where it comes from."""
        if self._v3:
            from psycopg.types.json import Jsonb
            return Jsonb(marked.value)
        import psycopg2.extras
        return psycopg2.extras.Json(marked.value)

    def _cursor(self, conn):
        if self._v3:
            import psycopg.rows
            return conn.cursor(row_factory=psycopg.rows.dict_row)
        import psycopg2.extras
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def _run(self, method, url, headers=None, params=None, data=None, timeout=None):
        path = str(url).rsplit("/rest/v1/", 1)[-1].split("?")[0]
        prefer = ((headers or {}).get("Prefer") or "")
        body = None
        if data is not None:
            body = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)

        if path.startswith("rpc/"):
            fn = path[4:]
            sql, args, wants = "SELECT * FROM %s()" % ident(fn), [], True
        else:
            sql, args, wants = build(method, path, params, body, prefer)

        conn = self._connect()
        try:
            with self._cursor(conn) as cur:
                cur.execute(sql, [self._json(a) if isinstance(a, JsonValue) else a
                                  for a in args])
                rows = []
                if wants and cur.description:
                    rows = [{k: jsonify(v) for k, v in dict(r).items()} for r in cur.fetchall()]
        except Exception as e:
            # Mirror PostgREST's shape: a 4xx with the driver's message, so db.py's existing
            # "read resp.text on failure" paths keep reporting something useful.
            try:
                conn.rollback()
            except Exception:
                pass
            return Response(400, {"message": str(e)[:400], "sql": sql[:200]})

        if method == "HEAD":
            n = (rows[0].get("n") if rows else 0) or 0
            rng = ("0-%d/%d" % (max(0, n - 1), n)) if n else "*/0"
            return Response(200, None, {"Content-Range": rng})
        if method == "POST":
            return Response(201, rows if wants else None)
        return Response(200, rows if wants else None)

    # requests-compatible surface. `head` carries no body by definition; the count rides the
    # Content-Range header, exactly as PostgREST does it.
    def get(self, url, **kw):
        return self._run("GET", url, **kw)

    def post(self, url, **kw):
        return self._run("POST", url, **kw)

    def patch(self, url, **kw):
        return self._run("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self._run("DELETE", url, **kw)

    def head(self, url, **kw):
        return self._run("HEAD", url, **kw)
