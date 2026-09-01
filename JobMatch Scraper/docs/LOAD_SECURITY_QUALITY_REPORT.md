# JobMatch: capacity, security and code-quality report

**Date:** 2026-08-12 · **Corpus:** 20,278 job rows

## ADDENDUM 2026-09-01 — the per-user row build, and what binds now

**Corpus: 38,805 live rows** (measured through the read-only proxy). The addendum below it was
taken at 21,982, so the per-request figures there are optimistic by roughly the ratio.

Measured on the same 1.8 GHz laptop, against the local 21,980-row snapshot unless stated. Still
not load-tested against production, for the reason in §1.

### What changed

`ranked_rows` rebuilt every card row per **(user, résumé)**. Of the 41 keys `_build_row` emits,
exactly one — `score` — depends on who is asking, so the other 40 are now built once per corpus
(`web._base_rows`) and the score is overlaid onto shallow copies.

| | before | after |
|---|---|---|
| per-user row build, repeat rebuild | 873 ms | **168 ms** |
| per-user row build, first in a worker | 1,941 ms | **198 ms** |
| feed render after an eviction or a prefs save | 933 ms | **201 ms** |
| feed render, fully warm | — | **11 ms** |
| `import web` (requests + bs4 deferred out of `core.py`) | ~1,200 ms | **~850 ms** |
| genuinely cold worker, first `/` | — | **~2.2 s** |

Output verified identical: 0 rows differing in any field and an identical order over all 21,960
rows, pinned in `scripts/test_speed_caches.py`.

### What binds now, and it is no longer the row build

The 2026-08-21 addendum found memory binding first, at **~31 MB of Python objects per additional
active user**, because each user's `_rows_cache` entry held the whole corpus. That entry still
exists, but two things changed:

- **`_cache_max()` went 5 → 4 entries**, because the shared build is now charged one entry against
  `CACHE_BUDGET_MB`. Leaving it out of the arithmetic would have grown the worker by a whole
  corpus with the budget none the wiser.
- **An eviction stopped being expensive.** It cost ~1.9 s of rebuild; it now costs ~200 ms. The
  concurrency ceiling is therefore much softer than the old "~8–10 distinct users at once" — past
  the cap, users are re-derived cheaply instead of re-scored.

**The remaining cold cost is the shared build itself (~1.4 s at 21,960 rows, more at 38,805), and
it is paid once per worker.** `/warm` exists to pay it off the user's path; `/healthz` never could,
because it has no user and builds nothing.

### Persisting the built rows: measured and rejected

1,360 ms to build against **282 ms** to read back from a **1.4 MB** gzip — so a `row_cache/` file
on the model of `score_cache/` would save ~1.1 s on a cold worker. Rejected: a built row embeds
`logo_url`, `sponsor_counts` and `visa_index` output, and **none of those three is covered by
`jobs_fingerprint()`**, so shipping new logos or sponsor data without a scrape would leave a file
serving stale badges indefinitely, where an in-memory cache dies with the worker. The saving is on
the one event the keep-warm cron exists to prevent; the hazard would be permanent. If more
cold-start speed is wanted, make `_build_row` cheaper — 62 µs/row, and it helps the warm path too.

(A JSON round trip also turns the `visa` **tuple into a list**. It is the only field that changes.)

### Front end

Two third-party origins left the critical path: a render-blocking stylesheet on
`fonts.googleapis.com` whose reply pointed at `fonts.gstatic.com` for the binaries. Six woff2 in
`static/fonts/`, 187 KB on disk, **78 KB actually fetched** (`unicode-range` means latin-ext is not
pulled in practice). Verified in composited Chromium: **0 requests to either Google host**, no CSP
violations, and **0 horizontal overflow at 375 / 768 / 900 / 1200 / 1440 / 1920**.

First-byte payload of `/`, unchanged in shape and worth recording: **100,492 bytes of HTML,
14,771 gzipped**, of which the 60-row inline bootstrap is 68,805 raw / **7,150 gzipped**. The feed
has shipped 60 rows inline plus server paging since before this pass — the payload was never the
problem.

Also removed: `will-change:transform` on every `.card`, which asked the compositor for a layer per
card (120+ on a paged feed) to serve a 3px lift one card uses at a time.

### Load test, 2026-09-01 — multi-process, and the thing that dominates is the cache cap

First run with **separate worker processes** rather than a thread pool in one: a feed render is
CPU-bound, so one process measures the GIL, not the pool. Harness committed as
`scripts/loadtest.py` — the first reproducible one this report has had.

**4 workers did not fit on the test box**: 297 MB RSS each against 947 MB free, so the runs below
are 2 workers (a real Passenger pool size) and 1 worker where memory forced it. 16 distinct users,
distinct résumés, `GET /` unless stated.

**S1 — 2 workers, default `CACHE_BUDGET_MB=256`.** Zero non-200s at every level.

| conc | rps | p50 | p95 | p99 | RSS/worker |
|---|---|---|---|---|---|
| 4 | 4.8 | 984 | 1,522 | 1,614 | 275 / 281 |
| 8 | **8.2** | 899 | 2,036 | 2,772 | 309 / 300 |
| 16 | 5.7 | 2,280 | 5,327 | 6,961 | 298 / 330 |
| 32 | 6.1 | 4,866 | 6,918 | 9,386 | 330 / 329 |
| 64 | 4.8 | 9,985 | 15,365 | 17,823 | 363 / 342 |

Knee at **concurrency 8, 8.2 rps on 2 workers (~4 rps/worker)**; past it latency climbs with no
throughput gain. Consistent with the 2026-08-12 closed-loop figure of ~5.2-5.6 rps/worker.

**S2 — THE HEADLINE, and it is not the scorer.** Same 16 users, 1 worker, concurrency 4:

| `CACHE_BUDGET_MB` | `_cache_max()` | rps | p50 | p95 | RSS |
|---|---|---|---|---|---|
| 256 (default) | 4 | 2.4 | 1,627 | 4,103 | 278 MB |
| 800 | fits all 16 | **50.2** | **76** | **119** | 524 MB |

**21x throughput and p50 1,627 -> 76 ms, for +246 MB per worker.** With more concurrently active
users than `_cache_max()`, every request evicts somebody and pays a rebuild; under it, nothing
does. **On production `_cache_max()` is 2 at 38,805 rows, so the third concurrent distinct user
triggers this.** Whether to raise it is a straight latency-for-memory trade and memory is the
binding constraint on shared hosting (§2.3) — recorded here as a measured option, not a
recommendation.

**S4 — post-scrape with `/warm` NOT run** (cron missed, or a worker spawned mid-window), 2
workers, concurrency 4: **1.0 rps, p50 3,545 ms, p95 11,468 ms.** This is the case `/warm` exists
to remove.

**S5 — `/api/feed?q=` surfaced a pre-existing 500. See below.** The rate limiter was deliberately
left on; the failures were 500s, not 429s.

### DEFECT: `_row_haystack` races itself, and search 500s under concurrency

`web.py:1827-1840`. Reproduced at **3 failures in 39 requests (~8%)** at concurrency 12 on one
worker, each a `KeyError` on a job URL:

```
_hay_idx["hay"][u] = h
_hay_idx["words"][u] = searchSplit(h)
return h, _hay_idx["words"][u]
```

`hay[u]` is written **before** `words[u]`, so any other thread that observes `hay[u]` in that
window takes the `h is not None` path and reads `words[u]`, which does not exist yet ->
`KeyError` -> **HTTP 500 from `/api/feed?q=`**. The wider window is the same: a thread that hits
the fingerprint-reset branch replaces both maps with fresh empty dicts while another is mid-fill.

Introduced 2026-08-12 in `a7bb62f`; none of this session's commits touch it. Production exposure
depends on whether Passenger serves concurrent requests per process — unknown, and worth
establishing, because the symptom is a 500 on every search rather than a slow one.

Not fixed here: this was a measurement run. The shape of the fix is to write `words[u]` last (so
observing `hay[u]` implies `words[u]` exists) and return the locally computed value rather than
re-reading the dict.

### The scorer, and the first render (same date, after the above)

The row build was not the dominant term on production; the **per-user scoring pass** was, because
every scrape invalidates every stored score file. Profiling found `_term_present` taking **76% of
the pass** at 3.2M calls with only ~50k distinct answers, and `score_against` building and fully
sorting `have`/`missing` on every row for consumers that are not on that path.

| scoring pass, per user, at 38,805 rows | |
|---|---|
| as shipped | **8.68 s** |
| `_term_present` memoised (96.8% hit rate) | 2.13 s |
| plus `core.score_pct`, the score-only path | **2.09 s cold-memo** *(0.77 s with the memo already warm)* |

Scores are **identical on all 21,494 analysed rows** across three rÃ©sumÃ©s, including one built to
force clean sweeps -- and that third one is load-bearing: a deliberately broken score_pct was
caught only by it, 6 rows at delta 1. Pinned in `scripts/test_speed_caches.py`.

**First render per user, measured end to end** over HTTP against the real app, worker warm on
shared state and the score file on disk, extrapolated x1.77 to 38,805 rows:

| | local (21,980) | at 38,805 |
|---|---|---|
| p50 | 356 ms | **628 ms** |
| p90 | 498 ms | **879 ms** |
| max | 590 ms | 1,041 ms |

Against **7,617 ms** locally before the change, and **15,659 ms** recorded on the live site. The
remaining per-worker cost is `_base_rows` at 3.84 s, which `/warm` absorbs off the user path --
the one request that pays it is the first to a brand-new worker.

Caveat on all of it: measured on a 1.8 GHz laptop and scaled by row count. Production is a shared
throttled slice and may be slower; the pre-change model predicted ~11 s against 15.7 s observed,
so treat these as the right order rather than the exact figure.

### Suite state at this date

**51 offline suites pass**, of 57 registered — the other 6 need database credentials and are
skipped by `run_tests.py` unless you pass `--db`. `scripts/feed_parity.py` reports **82/82 filter
cases agreeing** plus the `/api/feed` page walk. `scripts/build_docs.py --check` passes. The suite
counts in the sections below are what was true on *their* dates and are left as measured.

### Method note

`scripts/run_tests.py` reported every suite **2–4× slower** during this session (`feed_parity`
3.5 s → 15.6 s) purely from a concurrent session's load on the same machine, and one combined
command hit a 10-minute tool timeout that was contention, not a hang. **Don't read a timing
regression off a shared box** without checking what else is running.

---

## ADDENDUM 2026-08-21 — re-measured after the cPanel move and the score cache

**Corpus 21,982 rows.** Everything below is a fresh measurement, because two things invalidated
the numbers in the body: the database moved off Supabase onto cPanel Postgres (localhost, so the
round trips this report spends half its capacity waiting on are gone), and per-user scores now
persist to `score_cache/`.

Not load-tested against production, again and for the same reason (§1). Measured on a laptop
whose CPU was clocked at 1.8 GHz; production is a shared throttled slice and may be slower.

### Per-request cost, warm worker

| route | wall | CPU | rps/worker |
|---|---|---|---|
| `GET /` | 51 ms | 47 ms | 19.5 |
| `GET /api/feed` | 40 ms | 47 ms | 25.1 |
| **`GET /api/feed?q=`** | **173 ms** | 172 ms | **5.8** |
| `GET /job` | 96 ms | 94 ms | 10.5 |
| `GET /company` | 30 ms | 31 ms | 33.6 |
| `GET /healthz` | 1 ms | 0 ms | ~970 |

`GET /` went 197 ms → **51 ms**, and the idle share went **58% → ~8%**: that is the Postgres move,
and it is most of the capacity gain. **Search is now the expensive route** at 3.4× any other, so
three people typing cost more than fifty people reading.

On a realistic mix (weighted toward `/api/feed`, which every filter drag hits): **73 ms/request →
13.6 rps per worker → ~55 rps on four workers.** This report measured 56 rps on four workers after
its own fixes, so the model and the earlier load test agree.

### But memory binds long before throughput, and that is the real answer

| | |
|---|---|
| idle worker floor (imports + corpus) | **155 MB** |
| each additional distinct active user | **~31 MB** of Python objects, **~49 MB** RSS |
| at a 512 MB per-process cap | **~7–11 concurrent distinct users per worker** |
| at 1 GB | ~18–28 |

Passenger has no session affinity, so every worker eventually caches every active user. **So the
honest figure is ~8–10 people actively using it at once**, not the ~1,000 casual readers the rps
number implies. Past that it degrades rather than failing: an evicted user pays ~1.8 s for the
next feed render instead of 50 ms.

`_SCORE_CACHE_MAX = 64` was replaced by `web._cache_max()` as a result — a **byte** budget
(`CACHE_BUDGET_MB`, default 256) derived from the live row count. The old count cap permitted
~2.9 GB at this corpus and, being a count, got more dangerous with every scrape.

**Still unmeasured, and both are visible to you:** the production worker count and the
per-process memory cap (cPanel → Setup Python App, and Resource Usage). Those two numbers turn
the range above into a single figure.

---
> ## Fixed after this report was written
>
> All four items below were implemented and re-measured. **All 25 test suites still pass.**
>
> | fix | effect |
> |---|---|
> | Cache the profiles ROW; negative-cache `_research_for` | `GET /` **197 ms → 34 ms**, `/company` **284 ms → 85 ms**, `/job` **400 ms → 284 ms**. Supabase round trips per render: `/` 2→0, `/company` 3→0, `/job` 3→1. |
> | **Capacity** | **24 rps → 56 rps** on 4 workers, a **2.3×** gain. |
> | **1,000 people at once** (50 rps) | p50 **22.7 s → 1.1 s**, p95 38.2 s → 2.1 s. Now usable. |
> | Rate limiting on `/api/ext/*` | 40/hr for AI routes, 120/hr for bulk writes, 900/hr otherwise. Applied *before* auth, so token spraying is bounded too. |
> | `bump_token_epoch` | Raises on a failed read instead of resetting the epoch to 1 and un-revoking tokens. |
> | CSRF | 22 routes now covered by one `before_request` hook; 21 forms carry the token; app.js sends the header. |
> | CI | New `python-tests.yml` runs 21 suites on any Python/template/asset change — each one verified to work with the network physically blocked. |
>
> The findings below are preserved as they were measured, so the before/after is auditable.

Everything below is measured, not estimated, unless it says otherwise. Load testing ran against a
**local** instance because the site is on shared cPanel hosting where account-level throttling is
enforced above the app and no restart clears it. Production received only a capped `/healthz`
probe. No credentials were used and nothing was exploited against the live site.

---

## 1. Headline

| question | answer |
|---|---|
| How much traffic can it take? | **~24 requests/sec on 4 workers**, p95 521 ms. Breaks between 24 and 28 rps. In people: **~1,400 casual readers, ~480 browsers, ~120 heavy filterers.** |
| What about 1,000 people at once? | Fine if they read a page a minute (17 rps). At a page every 20 s (50 rps) the site does not crash — it **queues**, and everyone waits **23–41 seconds**. That needs 10–12 workers. |
| What breaks first? | Nothing broke. Zero errors at every level tested, locally and in production. |
| Biggest capacity waste | **43–58% of every page render is a worker sitting idle waiting on Supabase** — for 2–5 KB of data. |
| Biggest single risk | One user saving preferences costs **5.2 s of CPU** on a worker; one résumé edit costs **12.2 s**. Both scale with the number of users cached on that worker (up to 64). |
| Security | No critical vulnerabilities. Nine findings, ranked below. The fundamentals — password hashing, SSRF defence, XSS escaping, CSP — are genuinely well built. |
| Tests | **25 of 25 suites pass.** The problem is that CI runs only one of them. |

---

## 2. Capacity

### Throughput scales cleanly with workers

Local rig: N independent worker processes (`threaded=False`, matching Passenger's process model),
closed-loop client, sticky sessions, mix of `/`, `/api/feed`, `/company?c=Amazon`. 18–20 s per
level after warm-up.

| workers | peak throughput | knee at concurrency | per worker |
|---|---|---|---|
| 2 | 10.3 rps | 3 | 5.2 rps |
| 4 | 21.5 rps | 4 | 5.4 rps |
| 6 | 33.6 rps | 6 | 5.6 rps |

The knee lands exactly on the worker count every time, which is what a one-request-per-process pool
predicts. Past it, latency climbs with no throughput gain — at 4 workers, p99 went 512 ms → 2,764 ms
→ 3,596 ms as concurrency went 4 → 8 → 12, while throughput stayed flat at ~20 rps.

### The real ceiling, measured open-loop

The closed-loop numbers above understate capacity, because a closed-loop client slows its own
request rate to match the server. An **open-loop** generator — issuing on a fixed schedule whether
or not the server keeps up, and timing from the moment the request was *due* rather than when it
was sent — is what a crowd actually does. The first level of every run is discarded: it pays the
per-new-user cache build (~1 s each) and is not steady state.

4 workers, page mix, latency measured from the click:

| arrival rate | utilisation | p50 | p95 | verdict |
|---|---|---|---|---|
| 16 rps | 74% | 249 ms | 398 ms | comfortable |
| 20 rps | 93% | 262 ms | 394 ms | comfortable |
| **24 rps** | 111% | 298 ms | **521 ms** | **comfortable — the ceiling** |
| 28 rps | 130% | 1,758 ms | 3,298 ms | unusable |

**4 workers serve ~24 requests/second with a p95 of 521 ms, and fall over between 24 and 28.**
The cliff is sharp: one step past the ceiling, p95 goes from 0.5 s to 3.3 s.

**In people**, at 24 rps:

| how fast they click | people supported |
|---|---|
| a page every 60 s (reading a job description) | **~1,400** |
| a page every 20 s (browsing) | **~480** |
| a page every 10 s | ~240 |
| a page every 5 s (dragging filters) | ~120 |

### What 1,000 people actually looks like

Tested directly against 4 warm workers:

| scenario | demand | result |
|---|---|---|
| 1,000 people, page every 60 s | 16.7 rps | **fine** — p95 ~400 ms |
| 1,000 people, page every 20 s | 50 rps | **p50 22.7 s, p95 38 s, max 41 s** |

At 50 rps nothing errored and nothing crashed — every one of 1,501 requests completed. It simply
**queued**, and the backlog grew continuously: by the end of a 30-second run, requests were 40
seconds behind. That is the failure mode to expect. Not a 500 page — a page that arrives long after
everyone has given up.

Doubling to 8 workers only halved it (p50 12.9 s), because 8 × 5.4 = 43 rps is still short of 50.
1,000 people clicking every 20 s needs roughly **10–12 workers**, which is more than this host is
likely to run.

These numbers came from a laptop with 8 cores contending against the load generator, so they are
indicative rather than a production guarantee — production CPU is a shared, throttled slice.

### Production: at least 8 concurrent, and the worker count is not measurable this way

Capped probe on `/healthz` (2-byte response, no DB, no analytics):

| concurrency | throughput | p50 | p95 | errors |
|---|---|---|---|---|
| 1 | 1.42 rps | 654 ms | 747 ms | 0 |
| 2 | 3.17 rps | 648 ms | 720 ms | 0 |
| 4 | 6.33 rps | 649 ms | 680 ms | 0 |
| 8 | 12.67 rps | 652 ms | 700 ms | 0 |

Throughput scaled **linearly to 8** and latency stayed **flat at ~650 ms** — no queueing, no errors.
So the pool absorbs at least 8 concurrent requests, and **this probe cannot determine the worker
count**; finding it would need load I agreed not to apply. Reported as unmeasured rather than
guessed.

That ~650 ms is a fixed network/TLS round-trip floor from this location — it did not move under
load, and `/login` costs the same as `/healthz` despite doing real work. Application time is not
what a visitor is waiting on for cheap routes.

### 2.3 The amplifiers — where an outage would actually come from

**Cache wipes are expensive and blast a whole worker.** Six users warm on one worker, steady state
127 ms:

| trigger | next render, per user | worst | CPU added to that worker |
|---|---|---|---|
| `_rows_cache.clear()` — a successful `POST /prefs` (`web.py:2316`) | 889–1,295 ms | **10.2×** | **5.2 s** |
| `_bust_profile()` — résumé/story/lesson edit, ~8 routes (`web.py:484`) | 1,974–2,626 ms | **20.7×** | **12.2 s** |

At 5.4 rps/worker, one preferences save consumes roughly **28 requests' worth of capacity**. The
cache holds up to 64 distinct users (`_SCORE_CACHE_MAX`), so on a busy worker the same action costs
proportionally more — order **2 minutes of CPU** at a full cache.

**Cold start is 15–26× a warm render.**

| | cold | warm | ratio |
|---|---|---|---|
| 1 worker starting alone | 3,230 ms | 211 ms | 15× |
| 4 workers starting together | 4,106–4,131 ms | 151–169 ms | **26×** |

Starting together makes each one *worse*, because they contend for CPU during the scoring pass.
Passenger spins workers down after a few idle minutes, so this is the normal experience for the
first visitor after a quiet spell — which is exactly what the `/healthz` keep-warm pinger exists to
prevent. That pinger is load-bearing, not optional.

### 2.4 Memory

Measured RSS per worker, local: **145 MB** after import, **240 MB** warmed, up to **307 MB** under
load. Holding 6 cached users pushed one worker to **347 MB**.

Production should be lighter: `jdmeta.json` is gitignored and never deployed, and it accounts for
**83 MB** of the local heap. Estimated production ≈ **158 MB/worker warmed**, so 4 workers ≈ 630 MB
and 6 ≈ 950 MB. Shared accounts commonly cap at 512 MB–1 GB, so **memory is the plausible ceiling
on worker count** — but this is the one headline number I could not verify directly, because the
production account's limit isn't visible from outside. `/admin/health.json` or cPanel's resource
usage page would confirm it in seconds.

Note the trade-off runs both ways: without `jdmeta.json`, production saves that memory but runs the
*slower* scoring path (`unpack_analyzed` per row instead of a dict lookup), so production is lighter
in RAM and heavier in CPU than the local figures above.

---

## 3. Where the time actually goes

Per-request breakdown with the Supabase layer instrumented (median of 7, warm caches):

| route | wall | CPU | blocked on DB | DB calls | DB bytes | **% idle** |
|---|---|---|---|---|---|---|
| `GET /` | 197 ms | 94 ms | 114 ms | 2 | 2.8 KB | **58%** |
| `GET /job` | 400 ms | 234 ms | 170 ms | 3 | 5.1 KB | **43%** |
| `GET /company` | 284 ms | 109 ms | 154 ms | 3 | 1.7 KB | **54%** |
| `GET /api/feed` | 20 ms | 16 ms | 0 | 0 | 0 | 0% |
| `GET /api/feed?q=` | 81 ms | 78 ms | 0 | 0 | 0 | 0% |
| `GET /healthz` | 2 ms | 0 | 0 | 0 | 0 | 0% |

On a process-per-request pool, blocked time occupies a worker exactly as fully as CPU does. **Half
your capacity on the page routes is spent waiting for a few kilobytes.** This is latency, not
bandwidth — so it is a caching problem, not an egress problem.

Two specific, avoidable duplications, traced to the line:

**`GET /` fetches the same `profiles` row twice** — 1,431 bytes each, 46 ms + 54 ms:
```
web.py:1636  feed -> _user_prefs -> db.get_profile
web.py:1654  feed -> db.get_profile          (the visa nudge)
```
`_profile_cache` already exists in `web.py` with a 60 s TTL. Both paths bypass it. Routing them
through it removes ~100 ms from a 197 ms request.

**`GET /job` fetches the same `brain_companies` row twice** — 180 bytes each, 49 ms + 43 ms.
`_research_for` (`web.py:1727`) tries two domain spellings and pays a round trip for each. Both miss
(this company has no research record) and there is no negative cache, so every render of every job
at that employer pays 92 ms to re-learn nothing.

`/api/feed` is the well-behaved route: 20 ms, zero database calls, entirely served from cache. It
shows the ceiling the others could reach.

---

## 4. Security

No critical vulnerabilities. Ranked by exploitability; every item cites a line and says how it was
confirmed.

### Worth fixing

**1. No rate limiting on any `/api/ext/*` route** (`web.py:5016`–`5818`) — *read*.
A stolen extension token allows unbounded spend on **your** Anthropic/Gemini keys (`/api/ext/tailor`
also spawns a LaTeX subprocess per call), unbounded writes into the shared jobs table, and unbounded
appends to `ext_debug_log.jsonl`, which has no rotation. Login *is* rate-limited; these are not.

**2. Extension tokens never expire and travel in query strings** (`web.py:5067`, `:5139`, `:5365`) —
*read*. They land in access logs, proxy logs, `Referer` headers and browser history. They unlock
full PII including EEO data — gender, race, veteran and disability status (`web.py:5119`). Revocation
exists but only via epoch bump.

**3. `bump_token_epoch` can silently un-revoke tokens** (`db.py:1048-1058`) — *confirmed by reading
it*. `cur = 0`; a failed read is swallowed by `except: pass`; it then writes `cur + 1`. So a read
failure during a revoke rolls the epoch **backwards** (5 → 1) and re-validates tokens that were
already revoked. The docstring defends a *lost update* between racing bumps, which is sound — but it
does not cover the read-failure path, which is the one that matters.

**4. `SESSION_COOKIE_SECURE` defaults off** (`web.py:138`) — *read; production state unverified*.
Opt-in via env. The session cookie — which also carries a user's Gemini API key, since Flask
sessions are signed but **not encrypted** — can travel in cleartext unless you set it. I could not
check this remotely: Flask only emits the cookie once something is written to the session.
`/admin/health.json` reports this flag.

**5. 22 of 40 non-admin POST routes have no CSRF token** — *confirmed by enumerating every route*.
Includes `POST /profile`, which rewrites 39 fields and **blanks every field it omits**, plus
`/board/delete`, `/application/delete` and seven `/brain/*` delete routes. Ten `/api/ext/*` POSTs are
excluded (token-authenticated, CSRF does not apply); six routes *are* protected. `SameSite=Lax` plus
CSP `form-action 'self'` are real mitigations, so this is moderate rather than critical. The smell is
the inconsistency: the same file protects `/profile/tracking` and `/profile/revoke_token` but not
`/profile`.

### Lower priority

**6.** `probe_board` bypasses the SSRF wrapper on its workday/jibe branches — raw `SESSION.get`, no
public-IP check, no redirect re-validation, no size cap (`scraper/__init__.py:4611`). Reachable from
`/add` and `/api/ext/detect_board`.
**7.** The entire `os.environ` — including `SUPABASE_KEY`, `APP_SECRET` and AI keys — is handed to
the Tectonic subprocess that compiles user-derived LaTeX (`resume_brain/latex.py:269`). Not directly
exploitable (shell-escape is off by default) but the blast radius of any escape is every secret.
**8.** `MIN_PASSWORD_LEN = 6` (`auth.py:27`), below the NIST 800-63B floor of 8 that the adjacent
comment cites.
**9.** `_ext_user`'s docstring claims the HMAC is verified before the account cache is touched; it
isn't — `_ext_token` resolves the epoch first (`web.py:4581` vs `:4568`). The code is fine; the
documented security property is false, which is worse than not claiming it.

### Genuinely well built

Worth saying plainly, because it is unusual: PBKDF2-HMAC-SHA256 at 200k iterations with
`compare_digest`; login rate limiting that short-circuits **before** the expensive hash; uniform
error messages and a disabled-check ordered to prevent account enumeration; an open-redirect guard
on `?next=`; an SSRF wrapper that resolves DNS, requires *all* addresses public, re-validates every
redirect hop and caps body size; no SQL string building anywhere; résumé uploads that **never touch
disk**, which removes the entire path-traversal class; and `jdrender.py`'s escape-last architecture
where escaping is provably the final step. The production header set is strong — HSTS, nonce-based
CSP with no inline handlers, `frame-ancestors 'none'`, `object-src 'none'`, nosniff.

---

## 5. Code quality

**All 25 test suites pass.** Run with `EV_OFF=1`; 143 s wall.

```
ROOT (9 suites, self-contained)      all pass — 32 canonical-url, 26 jobspy, 17 résumé-upload,
                                     13 date-source, 10 title-filter, 7 verify-queue,
                                     6 workday-date, 7 password-rule, 5 scoring
scripts/ (15 suites)                 all pass — incl. 68 contrast pairs, feed_parity 79/79,
                                     jdrender, search+similar, job_page, onboarding, notify
node (1 suite)                       all pass — 72 ATS-adapter checks
```

Two suites print a `EOF marker not found` warning from pypdf on stderr; both genuinely pass.

**The real finding is that CI runs almost none of this.** `.github/workflows/web-build.yml` is
path-filtered to `web/**` and `static/dist/**`, so a change to `web.py` (5,852 lines), `core.py`,
`db.py` or `scraper/__init__.py` triggers **no workflow at all**. The 123 root test functions have
never run in CI. `feed_parity.py` and `test_canonical_url.py` are self-contained, need no
credentials, and would run today with nothing more than a workflow step.

**A failed save returns HTTP 200.** `save_prefs` returns `{"ok": false}` with status 200 when the
profile write fails (`web.py:2310`). Found the hard way: an amplifier test appeared to show no cache
wipe, because the POST had silently failed. Any caller checking the status code cannot tell success
from failure.

**Error handling:** 270 `except Exception` handlers, 49 whose body is exactly `pass`. Most are
correct by design (analytics must never break a request). Three hide real failures: `board_delete`
swallows every error then redirects to a page still showing the board (`web.py:4425`); the tailored
cache write returns `ok: True` regardless (`web.py:5265`); and the Supabase→local-JSON fallbacks
never report that they fired (`db.py:1685`, `:2019`) — on cPanel that file is ephemeral, so a
persistent outage degrades to writing where nobody reads.

**Absent tooling:** no linter, no formatter, no type hints (0 of 546 functions annotated), no
lockfile. `flask`, `requests` and `lxml` float unpinned in the production requirements with no
`pip-audit` and no Dependabot — a Flask 4.x release landing between two deploys would break the app
with nothing to roll back to.

---

## 6. What I'd do first

Ordered by value per unit of effort, not by severity.

1. **Route the two `db.get_profile` calls on `/` through the existing `_profile_cache`.** Removes
   ~100 ms from a 197 ms request — half the page — using a cache already in the file.
2. **Negative-cache `_research_for`.** Removes 92 ms from every `/job` render at any employer with
   no research record.
3. **Rate-limit `/api/ext/*`.** Currently the only unbounded spend path against your own API keys.
4. **Add a CI step running `feed_parity.py` and the 9 root suites** on changes to `web.py`/`core.py`/
   `db.py`. They already pass, need no secrets, and take under a minute.
5. **Set `SESSION_COOKIE_SECURE=1`** if it isn't already, and check `/admin/health.json`.
6. **Fix `bump_token_epoch`** to fail loudly rather than write `1` after a failed read.
7. **Reconsider the cache-wipe blast radius.** `_bust_profile` costing 12 s of worker CPU is the
   most likely cause of a future "the site froze for a minute" report.

Items 1, 2 and 7 are the capacity story; the rest is hygiene. None of them is architectural — the
design is sound, and the losses are in a handful of specific, fixable places.

---

## Appendix: how to reproduce

Harness scripts are in the session scratchpad (`.../scratchpad/qa/`), not in the repo:
`run_suites.py` (all 25 suites), `attribute.py` (CPU vs DB split), `trace_db.py` (per-call trace),
`loadapp.py` + `rig_a.py` (N-process capacity rig), `amplifiers.py`, `prod_probe.py`.

Every figure above states its sample count and percentile. Load runs used a frozen copy of
`jobs_snapshot.json.gz` with its mtime refreshed per level so the 3,600 s TTL could not expire
mid-run, `EV_OFF=1` so no synthetic analytics reached the live tables, and `RESEARCH_ON_DEMAND=0`
so no 12-second crawl could land inside a measurement.
