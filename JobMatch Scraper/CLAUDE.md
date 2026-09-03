# JobMatch — read this first

A personal job-search app: Flask (`web.py`) + a scrape/score pipeline (`scraper/`) + a résumé
grader (`resume_score.py`, `resume_brain/`) + a Chrome extension (`extension/`). Live at
`stemjobs1.astrochakra.co` on shared cPanel.

**Navigate with [docs/INDEX.md](docs/INDEX.md)** — symptom → file:line → the test that guards it.
[docs/MAP.md](docs/MAP.md) is the full symbol map. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is
how it works. [docs/OPERATIONS.md](docs/OPERATIONS.md) is how to deploy, test, and add things.

Everything below is here because *not* knowing it has already cost real time.

## Where you are

The working directory is this app dir; **the git root is the parent**, which also holds
`.github/workflows/` and two unrelated projects. `.env` and the data files (`companies.json`,
`sponsors.txt`, the sponsor JSONs) are read **relative to the current working directory** — a
script that doesn't `cd` here first silently gets a different database backend and reports
success. `DB_REQUIRE` makes that fail loudly instead.

## Before you run anything that imports `web`

**`EV_OFF=1` in the environment.** `analytics.py:33` reads it once at import into `_OFF`, so
setting it afterwards does nothing. One unguarded `feed_parity.py` run wrote 98.8% of all recorded
`feed_view` events. `scripts/run_tests.py` handles this for you.

## Editing traps

- **Line endings: the index is LF, the working copy is mostly CRLF.** `core.autocrlf=true`, so
  git normalizes on commit — but a script that reads a working-copy file with universal newlines
  and writes `\n` leaves the *whole file* looking rewritten locally, burying the real change in
  `git status` and your editor. Write back what you read, or write CRLF.
- **`static/app.js` is invisible to ripgrep** — **two** raw NUL bytes make it report
  `binary file matches` with no line numbers or content. They are deliberate: `_withinMemo`
  (~line 733) builds its cache key by joining a, b and k with a real 0x00 between each,
  chosen because it cannot occur in either operand. Described rather than quoted as a byte
  offset, because the offset moves with every edit above it — this file said 46372 when it
  was 47063. Git also classifies the file `-text` (binary), so `git diff` won't show it
  either. Use `grep -a` (GNU grep handles it fine), `git grep`, or `docs/MAP.md`. It is the
  second-most-coupled file in the repo.
- **`companies.json` is a shipped runtime asset**, not a cache. It is in `build_deploy_zip.py`'s
  **required** `FILES` and on `.cpanel.yml`'s `cp` line; without it `/companies` renders nothing.
  Rebuild with `python scripts/build_companies.py` after touching `SOURCES` or `sponsors.txt`
  **or adopting boards** — its universe is SOURCES + the `boards` table + `sponsors.txt` +
  corpus spellings, so an adoption run stales it and nothing says so. Point it at the live
  database (`DB_REQUIRE=proxy` + the `DB_PROXY_*` pair) and run `--check` — it fails if a
  high-traffic employer landed in `Unsorted`.
- **The logos are ours, harvested and committed to `static/logos/`.** Nothing is fetched from a
  third party at request time and `web.py`'s CSP `img-src 'self' data:` enforces it. Rebuild with
  `python scripts/build_logos.py` (sequential on purpose, resumable, no `--workers` — 12 threads
  measured 84% MISS against 96% paced) and gate with `--check`. Every candidate is judged on its
  **pixels**, because the chain this replaced asked a favicon service that answers HTTP 200 even
  when it has to invent the icon: 53% of tiles were not a usable brand logo.
- **`careers_us.md` is a build input now, not a runtime asset.** It stopped being deployed on
  2026-08-22: `/careers` is a 301 to `/companies` and nothing reads the file at request time.
  `scripts/build_companies.py` reads it for its hand-curated careers URLs, so it is still
  source-of-truth — just not shipped. Edit it through `scripts/build_careers_md.py`.
  **`scraper/make_careers.py` is gone.** It had no `if __name__ == "__main__"` guard, so merely
  *importing* it rewrote `careers_us.md` — reintroducing 622 `**` lines the renderer couldn't
  parse and wiping every in-feed marker. Two docs still told you to run it. If you add a
  generator to `scraper/` or `scripts/`, guard it.

## The filter triplet — the highest-consequence coupling here

"Does this job match this search" exists **three times** and all three must agree:

| Where | What |
|---|---|
| `web.py::_filter_rows` | the server feed |
| `static/app.js::matches()` | the client feed |
| `core.py::prefs_match` | the email digest |

The server/client switch is `web.py::_FEED_INLINE_MAX` (4000). Change one, change all three, then
run `EV_OFF=1 python scripts/feed_parity.py`. It lifts functions out of `app.js` **by source
text**, so a helper must stay a top-level `function`, not a `var`. There are ~16 such twins;
`web.py::_build_row` and `static/app.js::cardHTML` are the other pair that matters most.

## Entry points

`python web.py` locally, `passenger_wsgi.py` on cPanel. **`app.py` is retired Streamlit** — it
still boots, which is the trap; nothing imports it and it is in no deploy list.

## Deploy is a zip, not a push

`git push` deploys nothing. cPanel runs `.cpanel.yml` only for a repo hosted on cPanel, and origin
is GitHub. Use `python scripts/build_deploy_zip.py`, then upload/extract in File Manager and
`touch tmp/restart.txt`. Full steps in `docs/OPERATIONS.md`.

## Database

**Two** transports behind one interface, resolved in `db.py::_LazyHTTP`: `PG_DSN` → direct
psycopg (the cPanel app); `DB_PROXY_URL` + `DB_PROXY_SECRET` → HMAC HTTPS (Actions, your laptop);
neither → local CSV. A **half-set** `DB_PROXY_*` pair raises rather than falling through, and so
does asking for a session with nothing configured at all.

`db.has_remote_db()` means "is there a remote database at all" and answers True for both.
`db.backend_name()` is the one that tells you which.

**There was a third, and it was the default.** An unauthenticated Supabase REST session, removed
2026-09-01. It resolved from `.streamlit/secrets.toml`, so *any* process started here without
`PG_DSN` or `DB_PROXY_*` silently read and wrote the database this project left on 2026-08-15 —
including `scripts/dump_schema.py`, which had been printing that database's schema as "live" for
two weeks. If you are reading old code or docs that mention it: `using_supabase()` was renamed to
`has_remote_db()` (99 call sites) because a predicate named after a backend that no longer exists
is worse than a wide diff.

**`APP_SECRET` is now required whenever a remote database is configured.** The session key used to
fall back to `sha256(SUPABASE_KEY)`; with that gone, the only fallback left is a machine-local dev
value derived from the hostname and file path, which is guessable — so `web.py` refuses to import
rather than sign cookies and extension tokens with it.

- **`db.load_jobs()` with no `cols` downloads ~130 MB** of descriptions. Four scripts have done
  this; `db._warn_full_jd_read` is the tripwire it added.
- **`db._fetch_all` overwrites `limit`** with its 1000-row page size, so asking for one row walks
  the whole table. Copy the shape of `db.newest_event_ts()` for a one-row select.

## After you change code

```bash
python scripts/run_tests.py --changed   # only the suites your diff can affect
python scripts/build_docs.py            # regenerate the index; CI fails if it's stale
```

There is no pytest — every suite is a plain script (`python test_title_filter.py` works).

## Judgement calls already made — don't undo these

- **Don't make the match number relative.** A percentile answers a different question than the
  label asks and it saturates: since the feed sorts on this number, the top 40 rows all read 100
  and ranking is destroyed. If it needs rescaling, rescale what it measures (`core.core_terms`).
- **Don't reach for a job aggregator.** Adzuna was removed 2026-08-16: at 6% of the feed it was
  38% of every job with no usable description. Reasoning in `docs/OPERATIONS.md`.
- **One hue, and the card answers one question.** Colour used to mean *which* sponsorship route.
  As of 2026-08-31 every card wears the same blue and shows a single chip — "Sponsorship likely"
  / "Sponsorship unlikely", a star for a top H-1B sponsor, and no chip at all where there is no
  filing record. Both at the owner's direction after seeing the live feed. `static/style.css`
  states the rule (in the `--route-*` block, where only the h1b trio holds a value and the other
  four alias it) and `scripts/test_contrast.py` gates it in CI. Routes are still named in full on
  `/job` and `/companies`, which have room for them. Everything else is ink. **Don't reintroduce
  a per-route colour or a second chip on the card** — that has now been walked back twice.
- **A card field that isn't the score belongs to the POSTING, not to the reader.**
  `_build_row` emits 41 keys and exactly one, `score`, depends on who is asking. `_base_rows()`
  builds the other 40 once per corpus and `ranked_rows` overlays the score onto shallow copies:
  1,941 ms → 198 ms per user, byte-identical over all 21,960 rows. **`_dedupe_rows` must stay
  AFTER that overlay** — `_dupe_rank` tie-breaks on the score, so folding duplicates while every
  base score is 0 keeps a different copy. Moving it into `_base_rows` for speed passes every
  other test; `scripts/test_speed_caches.py` is the one that catches it.
- **The built rows ARE persisted now, and the key is the whole design.** This bullet used to
  say don't, on the grounds that a built row embeds `logo_url`, `sponsor_counts` and
  `visa_index` output and `jobs_fingerprint()` covers none of them. That objection was right
  and the conclusion was wrong: the fix is to put those inputs IN the key, not to throw the
  work away. `row_cache/*.rows.gz` is keyed on `(jobs_fingerprint(), _derived_signature())`.
  What made the old reasoning fail in practice: "`/warm` builds the shared half off the user's
  path" assumed one warm reaches every worker, and `/warm` is one HTTP request — Passenger runs
  several workers with no affinity and recycles them freely, so cold workers kept appearing and
  each one's first request paid the full build.
  Three rules that are each a bug someone already shipped:
  **(1) `_derived_signature()` hashes file CONTENT, memoised on the stat.** On mtime, a deploy
  — a zip extract, so every file rewritten and not a byte changed — invalidated all 40k rows
  and charged the first visitor ~7 s.
  **(2) Nothing that can fail silently may be in the key.** It once included two KV maps read
  over the network whose readers swallow a failure into `{}`; workers computed different keys,
  each rebuilt 7 s and overwrote the other's file. Production showed 63 ms and 8,401 ms in the
  same second.
  **(3) A request never writes the file; `/warm` (`persist=True`) and a from-scratch build do.**
  The gzip is most of the cost and charging it to whoever loads the feed next is the regression
  this replaced. `scripts/test_speed_caches.py` gates all three.
- **The fonts are ours too, and the CSP is what enforces it.** Six woff2 in `static/fonts/`,
  `@font-face` at the top of `style.css`. `font-src 'self'` with no remote origin left in
  `style-src` either — the same bargain as `img-src` for the logos. What this replaced was a
  render-blocking stylesheet on `fonts.googleapis.com` pointing at a *second* host for the
  binaries: two third-party handshakes in front of first paint, on the cold visit only, which is
  the whole shape of "slow the first time, fine the second". Gated by
  `python scripts/test_fonts.py`.
- **`db.list_users()` returning nothing is not "nothing to test."** That assumption is why the
  extension contract test sat outside CI for months.
- **Verify by behaviour, not substring.** Several past sessions reported false failures where the
  code was fine and the assertion was wrong.
- **Never load-test production.** Shared cPanel throttles at the account level and no restart
  clears it; the previous account was suspended once.
