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
- **`static/app.js` is invisible to ripgrep** — a raw NUL at offset 46372 (line 733) makes it
  report `binary file matches` with no line numbers or content. Git also classifies it `-text`
  (binary), so `git diff` won't show it either. Use `grep -a` (GNU grep handles it fine),
  `git grep`, or `docs/MAP.md`. It is the second-most-coupled file in the repo.
- **`companies.json` is a shipped runtime asset**, not a cache. It is in `build_deploy_zip.py`'s
  **required** `FILES` and on `.cpanel.yml`'s `cp` line; without it `/companies` renders nothing.
  Rebuild with `python scripts/build_companies.py` after touching `SOURCES` or `sponsors.txt`,
  and run `--check` — it fails if a high-traffic employer landed in `Unsorted`.
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

Three transports behind one interface, resolved in `db.py::_LazyHTTP`: `PG_DSN` → direct psycopg
(the cPanel app); `DB_PROXY_URL` + `DB_PROXY_SECRET` → HMAC HTTPS (Actions, your laptop); neither
→ Supabase → local CSV. A **half-set** `DB_PROXY_*` pair raises rather than falling through.

`db.using_supabase()` means "is there a remote database at all" and answers **True for all three**
— the name is historical. `db.backend_name()` is the one that tells you which.

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
- **Colour means sponsorship.** `static/style.css` states the rule and enforces a three-layer
  token system; `scripts/test_contrast.py` gates it in CI. Everything else is ink.
- **`db.list_users()` returning nothing is not "nothing to test."** That assumption is why the
  extension contract test sat outside CI for months.
- **Verify by behaviour, not substring.** Several past sessions reported false failures where the
  code was fine and the assertion was wrong.
- **Never load-test production.** Shared cPanel throttles at the account level and no restart
  clears it; the previous account was suspended once.
