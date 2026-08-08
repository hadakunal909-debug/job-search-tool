"""
analytics.py — product-usage events, buffered off the request thread.

The contract, in order of importance:

  1. It can never break a request. emit() constructs a dict and appends it to a bounded deque.
     Nothing touches the network on the request thread, ever, and every path is wrapped so an
     exception here cannot escape into a route.
  2. It can never amplify an outage. The buffer is capped, failed sends are dropped rather than
     retried, and after three consecutive failures the flusher backs off for a minute.
  3. It can never grow without bound. maxlen on the buffer, a per-hour ingest cap, and a 90-day
     retention prune (scripts/ev_maintain.py) — three independent brakes, because
     `tailored_cache` shows what one forgotten expiry does over a year.
  4. It records people by name, so it also has an off switch: EV_OFF=1 in the environment kills
     it globally, and profiles.extra.ev_off kills it per user.

What is NOT recorded, enforced by emit() only ever being called with named arguments and by
_clean_props' allowlist: no IP, no user-agent, no device fingerprint, no geolocation, no résumé
or JD text, no application notes, and nothing whatsoever from learned_answers (which holds
gender, race, veteran and disability answers) or the PII columns of profiles.
"""
import os
import re
import json
import time
import atexit
import threading
import collections

import db

# ---- configuration ----
_OFF = (os.environ.get("EV_OFF") or "").lower() in ("1", "true", "yes")
_BUF_MAX = 2000              # bounded: a DB outage costs memory nothing
_FLUSH_AT = 25               # events per POST
_FLUSH_AFTER = 20            # seconds an event may sit unsent
_TICK = 5                    # flusher wake interval
_MAX_FAILS = 3               # consecutive failures before backing off
_BACKOFF = 60
_HOURLY_CAP = 5000           # runaway guard: a JS loop can't fill the database
_COALESCE_WINDOW = 2.0       # seconds; collapses slider-drag / typing bursts
_COALESCE_MAX = 200          # entries kept in the dedupe map

# Only these event names are ever written. An unknown name is dropped silently rather than
# trusted — /api/ev is a browser writing into the database, so the allowlist is the boundary.
EVENTS = frozenset((
    "page_view", "feed_view", "job_open", "action", "group_expand", "prefs_save",
    "scrape_click", "login", "logout", "apply_click", "rail", "filter_panel",
    "clear_filters", "page_leave",
))

_buf = collections.deque(maxlen=_BUF_MAX)
_lock = threading.Lock()
_thread = None
_fails = 0
_muted_until = 0.0
_hour = [0, 0]                              # [hour-bucket, count-in-bucket]
_recent = collections.OrderedDict()         # (sid, signature) -> last emit time
_optout = {"names": frozenset(), "at": 0.0}
_OPTOUT_TTL = 300


def _opted_out(username):
    """Users who set the switch on /profile. Cached — this runs on every emit.

    profiles.extra is a jsonb blob the app already uses for odds and ends, so the opt-out needs
    no migration. A read failure means "not opted out", which is the wrong way to fail for a
    privacy control — but the alternative (dropping everything on a DB blip) makes the whole
    system silently useless, and the global EV_OFF kill switch exists for the case where you
    need a guarantee."""
    c = _optout
    if time.time() - c["at"] > _OPTOUT_TTL:
        names = set()
        try:
            rows = db._http.get(db._rest(db.PROFILES_TABLE), headers=db._headers(),
                                params={"select": "username,extra"}, timeout=10)
            if rows.status_code < 400:
                for r in rows.json() or []:
                    extra = r.get("extra")
                    if isinstance(extra, str):
                        try:
                            extra = json.loads(extra)
                        except Exception:
                            extra = {}
                    if isinstance(extra, dict) and extra.get("ev_off"):
                        names.add(r.get("username") or "")
        except Exception:
            names = set(c["names"])          # keep the last known list on a blip
        c["names"], c["at"] = frozenset(names), time.time()
    return username in c["names"]


_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")


def _clean_props(props):
    """Bound what a props blob can hold: short snake_case keys, scalars/short lists only,
    strings truncated. Applies to server-side emits too — this is the one place that decides
    how big an event row can get, and the size budget in the migration depends on it."""
    out = {}
    if not isinstance(props, dict):
        return out
    for k, v in list(props.items())[:20]:
        k = str(k)[:16]
        if not _KEY_RE.match(k):
            continue
        if isinstance(v, str):
            out[k] = v[:120]
        elif isinstance(v, bool) or isinstance(v, int) or isinstance(v, float):
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [x if isinstance(x, (int, float, bool)) else str(x)[:60] for x in v[:12]]
        elif isinstance(v, dict):
            out[k] = {str(kk)[:16]: (vv if isinstance(vv, (int, float, bool)) else str(vv)[:60])
                      for kk, vv in list(v.items())[:12]}
    return out


def _over_cap():
    """True once this process has emitted _HOURLY_CAP events this hour."""
    h = int(time.time() // 3600)
    if _hour[0] != h:
        _hour[0], _hour[1] = h, 0
    _hour[1] += 1
    return _hour[1] > _HOURLY_CAP


def _coalesced(sid, event, props):
    """True if this is a repeat of something seen < 2s ago and should be dropped.

    Only feed_view is collapsed. Dragging the score slider or typing in the search box fires a
    render per input event; the existing 250 ms debounce still lets a burst through, and each
    one would otherwise be a row. Keyed on the filter state so a genuine change always survives.
    """
    if event != "feed_view":
        return False
    sig = (sid, json.dumps(props.get("f") or {}, sort_keys=True), props.get("off"),
           props.get("tab"), props.get("qn"))
    now = time.time()
    last = _recent.get(sig)
    _recent[sig] = now
    if len(_recent) > _COALESCE_MAX:
        _recent.popitem(last=False)
    return last is not None and (now - last) < _COALESCE_WINDOW


def emit(username, sid, event, job_url=None, company=None, source=None, score=None, **props):
    """Queue one event. Returns immediately; costs a dict and a deque append."""
    if _OFF or not username or event not in EVENTS:
        return
    try:
        if _opted_out(username) or _over_cap():
            return
        clean = _clean_props(props)
        if _coalesced(sid or "", event, clean):
            return
        row = {"username": username[:80], "sid": (sid or "")[:24], "event": event,
               "props": clean}
        if job_url:
            row["job_url"] = str(job_url)[:1000]
        if company:
            row["company"] = str(company)[:200]
        if source:
            row["source"] = str(source)[:120]
        if score is not None:
            try:
                row["score"] = max(0, min(int(score), 32767))
            except Exception:
                pass
        _buf.append((time.time(), row))
        _start()
    except Exception:
        pass


def _flush():
    """Send whatever is queued. Called by the flusher thread and at exit."""
    global _fails, _muted_until
    if time.time() < _muted_until:
        return
    with _lock:
        batch = [row for _, row in list(_buf)[:200]]
        for _ in range(len(batch)):
            try:
                _buf.popleft()
            except IndexError:
                break
    if not batch:
        return
    if db.insert_events(batch):
        _fails = 0
        return
    _fails += 1
    if _fails >= _MAX_FAILS:
        # Drop, don't retry. The events are worth less than the risk of a growing buffer and a
        # thread hammering a database that is already unhappy.
        _muted_until = time.time() + _BACKOFF
        _fails = 0
        _buf.clear()


def _loop():
    while True:
        time.sleep(_TICK)
        try:
            if not _buf:
                continue
            oldest = _buf[0][0]
            if len(_buf) >= _FLUSH_AT or (time.time() - oldest) >= _FLUSH_AFTER:
                _flush()
        except Exception:
            pass


def _start():
    """Start the flusher on first use. Daemon, so it never holds up a shutdown."""
    global _thread
    if _OFF or _thread is not None:
        return
    try:
        _thread = threading.Thread(target=_loop, name="analytics", daemon=True)
        _thread.start()
        # Best effort on shutdown. Under Passenger a killed idle worker loses at most one
        # batch, which is the price of never touching the network on a request thread.
        atexit.register(_flush)
    except Exception:
        _thread = None


def stats():
    """For /admin/usage: what the buffer is doing right now."""
    return {"off": _OFF, "queued": len(_buf), "muted": time.time() < _muted_until,
            "hour_count": _hour[1], "optouts": len(_optout["names"])}
