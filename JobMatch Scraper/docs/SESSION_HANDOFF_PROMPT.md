# Handoff prompt — JobMatch, session of 2026-09-01

Paste this at the start of a new chat to resume with full context. It replaces the 2026-08-18
version, which predates the score cache, the one-hue feed and the per-corpus row build.

**Every number below was measured in this session** — against the live database through the
read-only proxy, or by running the code — except where it says *carried forward, unverified*.
Those are items from the previous handoff that are probably still true and that nobody re-checked;
treat them as leads, not as facts.

---

## The project

**JobMatch** — a personal job-search tool at `C:\Users\k.signhhada\Desktop\Job Planning & Research`.
The Flask app is in `JobMatch Scraper/`. Live at **stemjobs1.astrochakra.co** on shared cPanel
(LiteSpeed + Passenger). A Chrome extension in `extension/` fills application forms.

| | measured 2026-09-01 |
|---|---|
| live `jobs` rows | **38,805** (max `first_seen` 2026-09-01) |
| boards swept | **1,218** across **40** ATS adapters (`len(scraper.SOURCES)` / `len(scraper.SCRAPERS)`, measured 2026-09-08; was 1,192/38 here) |
| routes | **87** (86 handlers), no blueprints |
| `web.py` | 8,786 lines · `core.py` 3,272 · `db.py` 2,951 |
| `scraper/__init__.py` | 9,051 lines · `scraper/score_jobs.py` 2,094 |
| `static/app.js` | 1,904 lines · `static/style.css` 2,974 |
| test suites | **51 offline, all passing** (of 57 registered; 6 need db creds, `--db`) |
| `scripts/feed_parity.py` | **82/82 filter cases agree** |
| generated docs | 46 files, 2,076 symbols, 36 index rows, 5 known-wrong entries |
| self-hosted logos | 1,483 assets + `static/logos/index.json` |

The local `jobs_snapshot.json.gz` holds **21,980 rows dated 2026-08-17** — it is routinely stale
and that is fine; `.claude/devpreview.py` runs the real app offline against it.

### Where the database is
**cPanel Postgres, not Supabase.** Three transports behind one `db.py` (`_LazyHTTP`):

1. `PG_DSN` → `pgrest.Session` (direct psycopg). This is the cPanel app; the DSN is loopback-only.
2. `DB_PROXY_URL` + `DB_PROXY_SECRET` → `dbproxy.Session`, HMAC HTTPS to `POST /api/db`. This is
   how Actions and your laptop reach it.
3. Neither → Supabase, then a local-CSV fallback.

To read production from here:
```bash
cd "JobMatch Scraper" && EV_OFF=1 DB_PROXY_SECRET="$(tr -d '\r\n' < .db_proxy_secret)" \
  DB_PROXY_URL="https://stemjobs1.astrochakra.co/api/db" python your_script.py
```
`db.using_supabase()` answers True for all three — the name is historical. `db.backend_name()` is
the one that tells you which.

### Deploy — read this before advising anything
`git push` deploys **nothing**. `python scripts/build_deploy_zip.py` → upload in cPanel File
Manager → Extract → `touch tmp/restart.txt`. `.cpanel.yml` exists and has never run, because
cPanel only executes it for a repo hosted on cPanel.

**Build the zip from a detached worktree at a commit**, not from the working tree — the tree is
routinely shared with a concurrent session and `build_deploy_zip.py` bundles whatever is on disk:
```bash
git worktree add --detach C:/jmz HEAD    # SHORT path: long logo names blow Windows MAX_PATH
cd C:/jmz/"JobMatch Scraper" && EV_OFF=1 python scripts/build_deploy_zip.py
```

---

## What changed since the last handoff

Two Claude sessions worked in this repo through late August, **sharing one `.git`**. Commits
interleave; read `git log`, not the working tree.

- **Per-user scores persist to `score_cache/`** (2026-08-21). Scoring the corpus against one
  résumé is 5–15 s of CPU and was being paid by the first request each worker served for each
  user; a 13.8 s LCP on the live feed was all of it.
- **Off Supabase onto cPanel Postgres** (2026-08-15), which removed most of the idle share of a
  request.
- **Logos are harvested and committed** to `static/logos/`; the CSP is `img-src 'self' data:` with
  no remote origin. `/companies` replaced the old Sponsors page.
- **The feed went to one hue and one chip** (2026-08-31, owner's direction, walked back twice
  before that — don't reintroduce per-route colour or a second chip).
- **Sponsorship data refreshed** to USCIS FY2021-2025 + DOL FY2026 Q3, and the shipped FY2023 file
  turned out to be a **partial year** (33,332 rows against 57,415), so the old window was ~4.4
  years, not 5.
- **This session: the per-corpus row build.** `_build_row` emits 41 keys and exactly one, `score`,
  depends on who is asking. `web._base_rows()` builds the other 40 once per corpus and
  `ranked_rows` overlays the score: **1,941 ms → 198 ms per user**, byte-identical over all
  21,960 rows. Plus `/warm`, self-hosted fonts, and the feed's loading/motion states. Details in
  `git show 409823d` and in the three new rows in `docs/INDEX.md`.

Current timings, this laptop: feed render **11 ms warm**, **~2.2 s** on a genuinely cold worker
(all of which is the shared build, which `/warm` exists to pay off the user's path).

---

## Current state

- **HEAD is `e13d498`** on `ats-detection-and-company-discovery`, in sync with `origin`.
- All 51 offline suites pass (6 more are `--db`-gated); `build_docs.py --check` passes.
- `stemjobs1_deploy.zip` at the repo root was built from `4bbed05` and verified byte-equal to that
  commit. `e13d498` touched only `CLAUDE.md`, `README.md`, `docs/` and `scripts/` — **none of
  which ships** — so the zip is still current for every deployed file.
- **Nothing has been deployed.** The live site is serving the previous build.
- **CI status unknown.** `gh` is not authenticated on this machine, so nobody has read the Actions
  result for these commits. The two things CI gates — `build_docs.py --check` and the offline
  suite — both pass locally.

## Outstanding, in order

1. **Upload the deploy zip**, then `touch tmp/restart.txt`. Everything below the fold in this
   session's work is invisible until that happens.
2. **Set `WARM_TOKEN`** in the cPanel env and repoint the keep-warm cron at `/warm` (see
   OPERATIONS.md §Keep-warm). `/healthz` keeps a worker alive but builds nothing, so on its own it
   cannot remove the cold-start cost. Note `-m 60`, not `-m 20`.
3. **Re-paste `DB_PROXY_SECRET` in GitHub.** *Carried forward, unverified.* Nothing else restores
   verify_dates, the analytics rollup or the digest email.
4. **The `.env` DB vars on the box.** *Carried forward, unverified.*
5. **Seven companies have no board at all** — ASML, Deutsche Bank, LTIMindtree, Marlabs, Qualcomm,
   Renesas, Tradeweb. Adzuna-only, and Adzuna was removed 2026-08-16. *Carried forward,
   unverified* — later sessions adopted many boards, so re-check before acting.
6. `_research_for` still calls `logodomain(display)` with no URL. **Left alone deliberately** — it
   is a research cache key and changing it invalidates stored research.

**Dropped from the previous list:** *Set `LOGODEV_KEY`* is obsolete. The favicon chain it fed was
removed on 2026-08-22; logos are harvested once and committed, and the CSP now forbids remote
images outright.

---

## Gotchas that cost real time — don't rediscover them

- **`EV_OFF=1` before anything imports `web`.** `analytics.py` reads it once at import. One
  unguarded parity run wrote 98.8% of all recorded `feed_view` events. `scripts/run_tests.py`
  handles this for you.
- **Mixed line endings, and it is per file.** `web.py`, `core.py`, `style.css` and the templates
  are 100% CRLF; `static/app.js` and `db.py` are 100% LF. A patch script that reads with universal
  newlines and writes `\n` rewrites the whole file and buries the real diff. Detect the file's own
  ending and write it back.
- **`static/app.js` is invisible to ripgrep** — two deliberate NUL bytes in `_withinMemo`'s cache
  key (~line 733). Use `grep -a`, `git grep`, or `docs/MAP.md`. Don't quote a byte offset; it
  moves with every edit above it.
- **The filter exists three times** — `web.py::_filter_rows`, `static/app.js::matches()`,
  `core.py::prefs_match` — and `feed_parity.py` lifts the JS **by source text**, so a helper must
  stay a top-level `function`, not a `var`.
- **`_dedupe_rows` must run AFTER the score overlay.** `_dupe_rank` tie-breaks on the score, so
  folding duplicates while every base row still sits at 0 keeps a different copy. Moving the
  dedupe into `_base_rows` for speed passes every other test.
- **`jd_terms`'s `"n"` is the THIN FLAG, not a term count.** A fixture written `"n":2` makes
  `_row_pending` true, `_build_row` forces `score` to 0, and the failure surfaces somewhere else.
- **`_build_row` reaches the database in exactly two places**, both `db.get_kv` and both memoised:
  `_repost_count` and `_host_jd_blocked`. Seed **both** to make a test offline, and *count* the
  calls rather than reasoning about which exist.
- **`db._fetch_all` overwrites `limit`** with its 1000-row page size, so asking for one row walks
  the whole table. Copy `db.newest_event_ts()` for a one-row select.
- **`db.load_jobs()` with no `cols` downloads ~168 MB** of descriptions at this corpus size.
- **Never validate a commit by grepping the working tree.** `feed_parity` and grep read the tree,
  so with uncommitted edits present they compare it against itself. Diff against **HEAD**.
- **`git commit --only -- <paths>` commits the WORKING TREE for those paths, not the index** — it
  will silently sweep in a concurrent session's work. Stage the index, then `git commit` with no
  pathspec.
- **Don't make the match number relative.** A percentile answers a different question from the one
  the label asks, and since the feed sorts on it, the top 40 rows all read 100 and ranking is
  destroyed. Rescale what it measures (`core.core_terms`) instead.
- **`db.list_users()` returning nothing is not "nothing to test".** That assumption is why the
  extension contract test sat outside CI for months.
- **Verify by behaviour, not substring.** Several past sessions reported false failures where the
  code was fine and the assertion was wrong. A check that cannot fail is worse than none — prove
  a new guard bites before trusting it.
- **A comment is not the feature.** `test_card_meta.py` and `test_fonts.py` both had to strip
  comments first, because prose describing a removed thing read as the thing.
- **Never load-test production.** Shared cPanel throttles at the account level, no restart clears
  it, and the previous account was suspended once.
- **The browser pane may not composite** (`visibilityState:"hidden"`, `innerWidth:0`), which
  silently breaks screenshots, overflow measurement and every `loading="lazy"` image. Drive
  Playwright directly and read the PNG.
