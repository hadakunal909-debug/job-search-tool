# JobMatch

**What we are, what we do, and how it is built.**

*Live at [stemjobs1.astrochakra.co](https://stemjobs1.astrochakra.co). Every figure in this document
is measured, and each one says where it came from. Live database counts were read on **2026-09-12**
through the HMAC proxy; code counts come from `HEAD` the same day; and **§7 was read off the
production box over SSH on 2026-09-13 03:48–03:52 UTC** — the deployed commit, the crontab, the
worker memory, the real database size.*

*Where a figure is quoted from `OPERATIONS.md` or a past measurement rather than re-taken, it is
dated in place. §7 is the only section that describes the server as it is right now.*

---

## 1. What we are

JobMatch is a **job search engine built for one specific problem**: an international student in the
US who needs a job that (a) actually matches their résumé and (b) comes from an employer who will
sponsor a visa — and who cannot afford to find out which is which by applying to four hundred
postings one at a time.

It is not a job board, and it is not an aggregator front-end. It is a **vertically integrated
pipeline**: we scrape employers' own applicant-tracking systems directly, fetch the full job
description for every posting we keep, score each one against the user's actual résumé text, join
it against five years of federal immigration filings, and then hand the result to a browser
extension that fills the application form.

Three properties define the product, and each is a decision we have already had to defend at least
once:

| Property | What it means | Why |
|---|---|---|
| **First-party sources only** | We read 40 ATS platforms directly. No aggregator is ever a feed source. | Adzuna was 6% of the feed and 38% of every job with no usable description. Removed 2026-08-16. An aggregator's API returns a truncated blurb and its redirect pages refuse a server-side fetch, so a row sourced that way can never carry a real description or a real apply form. |
| **The match number is absolute, not relative** | 72% means "your résumé covers 72% of what this posting emphasises", not "you are in the 72nd percentile". | The feed *sorts* on this number. A percentile saturates — the top 40 rows all read 100 — and destroys the ranking it was meant to improve. |
| **Sponsorship is a signal, never a gate** | Absence of a filing record is not evidence of non-sponsorship. The feed is never filtered on it. | Most employers who would sponsor have simply never filed for a role like yours. Filtering on it would delete the opportunity set. |

---

## 2. By the numbers

### The corpus (live, 2026-09-12)

| | Count |
|---|---|
| Job postings held | **54,226** |
| …with a stored description | 53,979 (**99.5%**) |
| …with a packed keyword analysis (`job_terms`) | 53,950 |
| …with employer-stated facts extracted (`job_facts`) | 54,223 |
| …with a **verified** real posting date | 4,825 |
| Employers in the directory | **5,291** |
| …with at least one H-1B approval on federal record | **3,260** |
| Adopted job boards (the `boards` table) | **1,095** |
| Built-in boards (`scraper.SOURCES`) | **1,218** |
| **Effective scrape sources** | **~2,313** |
| Stored per-(user, job) match scores | **184,108** |
| Aggregator discovery findings banked | 2,760 |
| Applications tracked | 235 |
| Accounts | 9 (admin-created; there is no public sign-up) |

### The sources

1,218 built-in boards across **29 ATS platforms**, plus 1,095 adopted at runtime. **40 adapters**
exist in total — the extra eleven serve platforms that only arrived through app-side adoption.

```
  415  greenhouse        34  oracle             2  breezy
  246  workday           21  phenom             2  peoplesoft
  131  smartrecruiters    5  ultipro            1  amazon
  119  ashby              5  avature            1  kula
   81  successfactors     3  eightfold          1  pinpoint
   80  lever              2  recruitee        + 13 more with one board each
   58  jibe
```

### The codebase

| | |
|---|---|
| Python files / lines | 177 / **84,407** |
| `web.py` — every route and request hook, no blueprints | 10,304 lines · **88 routes** |
| `scraper/__init__.py` — the sweep, the intake filter, 40 ATS adapters | 10,198 lines |
| `core.py` — the shared domain spine | 5,607 lines · 40 sections |
| `db.py` — one interface, three live transports | 4,090 lines |
| `scraper/score_jobs.py` — description fetch + scoring | 2,570 lines |
| `static/app.js` — the client feed | 2,326 lines |
| `jdrender.py` — description → HTML | 1,681 lines |
| `resume_score.py` — the offline rubric | 1,615 lines |
| Jinja templates | 33 |
| Operational scripts | 86 |
| Test suites (all plain scripts; there is no pytest) | **71** |
| Tracked files | 2,364 |
| Commits, 2026-05-31 → 2026-09-12 | **606** |

### Performance

*These are recorded measurements from the 2026-09 speed work, not re-taken here. For numbers read
off the server today — a live `/warm` cycle, `/login` timings, worker memory — see §7.*

| | Cold | Warm |
|---|---|---|
| First feed render per user, at 38,805 rows | p50 **628 ms** / p90 879 ms | — |
| The same measurement before the 2026-09 work | 15,659 ms | — |
| Feed render, steady state | — | **80–170 ms** |
| Built rows for the whole corpus | 7,101 ms (full build) | **55 ms** (incremental) |
| `_base_rows` on a cold worker | 4,126 ms | **305 ms** (reads a 1.4 MB gzip) |
| `core.load_idf` — the 29.4 MB IDF table | 582 ms / 261 MB peak | **38 ms / 17 MB** |
| Per-user row overlay at 21,960 rows | 1,941 ms | **198 ms** |
| `_build_row`, per row, warm | 77.19 µs | **51.96 µs** |

The server itself answers in **2–5 ms**. What a visitor feels on top of that is network: the box is
in India, measured from this laptop at **271 ms RTT with 25% packet loss**.

---

## 3. What we do — the product

### 3.1 The feed

A fixed three-column grid of scored, de-duplicated, filtered job cards.

- **Match %** against the user's live résumé text — computed per user, against their own résumé,
  not against a list of keyword tags.
- **Filters**: match floor, role family, dev-vs-management track, location, remote, pay, years of
  experience, freshness, visa route, staffing-agency exclusion. **Saved as a search**, which then
  also drives the email digest and the extension's batch queue.
- **Employer floods are grouped, never de-duplicated.** Fifty real openings at one company are
  fifty real openings; collapsing them loses information the user wants.
- **Closed postings are detected and hidden**, not left to rot.
- **Repost clustering**: the same role re-listed under a new URL is recognised — and *location* is
  the key that makes that work, not the title.
- **Live scrape progress**: the in-app "Update jobs" button fires the GitHub Actions workflow and
  the page draws a real progress bar (boards done, jobs found, elapsed, ETA) off a shared status
  row, because shared cPanel cannot run a 15–30 minute scrape inside a web request.

**The card design is a stated rule, gated in CI.** One hue, and **one chip**. A chip is a
*verdict*; a fact is a *column*. The card is white with an outline, and the single blue is spent on
`.cardverdict` — a tinted column down the trailing edge holding the match ring, its label and the
sponsorship line. Everything the *employer* stated — pay, location, years — is ink in a fixed-track
grid, so pay sits under pay down the whole page. `scripts/test_contrast.py` enforces the colour
rule; the fixed three-column grid is what makes the fact cells share an offset (measured
`19,232,19,232` on all 60 cards).

Fact cells are ordered by **coverage, not importance** — location 99%, years 72%, pay 37%, remote
8%, re-measured 2026-09-08 over 35,754 active rows — so the 52% of cards carrying exactly two facts
fill line one instead of sitting diagonally opposite.

### 3.2 Sponsorship intelligence

The single biggest time-saver for an international student, and the part no general job board has.

- **H-1B filing strength** per employer, from the USCIS H-1B Employer Data Hub — **129,661 employer
  keys** over a **five-year window (FY2021–FY2025)**, 2.04 million approvals. Tiered into a
  confidence band (`core.sponsor_strength`), with a star for a top sponsor.
- **Visa route tags** — H-1B, H-1B1, E-3, STEM OPT, green card (PERM) — **123,473 employer keys**,
  from DOL LCA and PERM disclosure data (FY2026 Q3) plus the federal E-Verify employer list.
- **Cap-exempt employers** identified separately (universities, nonprofit hospitals, research
  institutes) — not subject to the H-1B lottery, which changes the calculus entirely.
- **Work-authorisation timeline** — given a graduation date and OPT status, what the runway
  actually looks like.
- **Staffing / body-shop flag**, so consultancies can be excluded if the user wants.

The window width is load-bearing: `sponsor_strength` tiers on **absolute** counts, so narrowing
five years to three cuts the `high` tier from 169 employers to 95.

### 3.3 The job page

One posting, read properly.

- The full description, **re-rendered into a fixed canonical section stack** — Role,
  Responsibilities, Requirements, Preferred, Benefits, About. 62% → **94%** of 4,000 real rows now
  render as multi-section rather than one wall of text.
- **Keyword panel**: which terms this posting emphasises, which your résumé already has, which it
  is missing — weight-ordered, so the terms the score is actually made of sit at the top.
- **Corpus norms**: what this *role* usually asks for, and which tools *this employer* leans on,
  learned from **48,339 postings**. Stated as a prevalence *difference*, not a ratio — a ratio floor
  manufactures significance out of rare terms.
- Sponsorship routes named in full (the card has room for one chip; this page has room for words).
- One-click tailoring straight into Resume Brain.

### 3.4 The résumé grader (`/resume`)

A 0–100 score, **offline**: no network, no API key, no model.

**25 weighted checks** across five categories — impact, voice, mechanics, structure and skills.
Three constraints are deliberate:

- **Deterministic.** Same text in, same score out, so "did my edit help?" is answerable by
  re-scoring. A grader whose number drifts is not a measurement.
- **Every penalty names the offending lines.** A score with no offenders attached is not actionable,
  and unactionable feedback is what makes most résumé graders useless.
- **Judgements that genuinely need a model are absent, not faked.** "Is this bullet an
  accomplishment or a restated duty?" cannot be answered with a keyword list, so we don't pretend
  it can.

There is an **impact gate** on top of the weighted average, because fifteen hygiene checks can
otherwise outvote the one thing that matters: a tidy résumé with nothing to say should not outscore
a substantive one with a typo.

### 3.5 Resume Brain

The tailoring layer — **and its core has no AI in it.**

A deterministic *plan* is produced first. It reasons over the user's complete profile (every
résumé, every stored story), the analysed job, and cached company research, then decides which
résumé to start from, which stories to feature, which keywords to mirror, and what voice the JD is
written in. Lessons from user feedback persist per-user and change future plans.

Only then, and only if the user asks for "write it for me" and supplies their own key, does an
optional AI layer render that plan into finished prose — Google Gemini or Anthropic Claude, chosen
by the shape of the key, both plain REST with no SDK. **It never decides what is relevant; the
brain already did. It only writes.** Truth rules and style rules live in one shared `voice.py`, so
the grader and the rewriter cannot disagree about what a filler word is.

Exports to `.docx` and `.pdf`.

### 3.6 The Chrome extension

Fills job application forms. **No AI is involved in applying, and nothing is ever auto-submitted** —
it fills what it can answer from your data, drops a review panel listing what it filled and what is
left, and *you* upload the résumé and click Submit.

- **Tuned adapters** for Workday, Oracle Cloud, iCIMS, Salesforce Experience Cloud, SuccessFactors,
  Greenhouse, Lever, Ashby and SmartRecruiters — together **~80% of the feed**. Everything else goes
  through a generic adapter that handles standard forms plus React comboboxes and custom Yes/No
  toggle widgets.
- **Vanity career domains are recognised by fingerprint, not hostname.** About **29%** of the corpus
  sits on employer hostnames (`careers.airbnb.com`, `jobs.sap.com`) fronting a stock ATS, which no
  host list can enumerate. Measured over the 30 largest such hosts in a real browser: **71%** resolve
  to a platform that has a tuned adapter.
- **Shadow DOM is pierced everywhere** — Salesforce Experience Cloud forms are invisible to any
  light-DOM scan.
- **A learned-answer bank.** It reads how *you* filled a form and replays it on the next form with
  the same question — and does so passively, on every submit. Sensitive fields (password, SSN, card,
  DOB) are always skipped.
- **Batch mode** loads exactly the jobs your saved search would show, newest first, one tab each.
- **It is also a scraper of last resort.** For an employer behind an Akamai bot-wall — Tesla is the
  classic, 403 to plain `requests`, to a spoofed Chrome TLS fingerprint, and to both headless and
  visible Playwright — your own browser has already passed the wall. "Import all jobs on this page"
  reads the live listing from inside it and sends the rows through the same title/US filter and URL
  dedupe as every scraped board. Disabled on LinkedIn/Indeed/Glassdoor, whose terms ban collection.

15 `/api/ext/*` routes back it, authenticated with an HMAC bearer token derived from `APP_SECRET`.

### 3.7 Everything else

- **Applications tracker** — status, dates, which résumé you sent, CSV export.
- **Company directory** (`/companies`) — 5,337 employers hand-curated into **14 sectors**, with
  self-hosted logos, filing counts and careers links.
- **Email digest** — `scraper/notify.py` sends one email per user, driven by the same saved search
  as the feed.
- **Admin console** — user management, usage analytics, data health, board blocking, cache reload.
- **React islands** — a separate Vite/TypeScript project building into `static/dist/`, committed
  because the server runs no build step.

---

## 4. How it works — the pipeline

### 4.1 Intake: a funnel whose gates announce themselves

A sweep reads ~2,300 boards, then puts every posting through an **ordered filter**. Most are
dropped, which is correct — the corpus is a small fraction of what the boards serve.

```
  scrape_all  →  fill_missing_jds()  →  [ 8 gates ]  →  last_new_jobs.json  →  insert
```

The gates, in the order they apply: already known → blocked company → off-target function title →
no matching role keyword → non-US location → older than the age limit → no federal sponsor record
(aggregator rows only) → aggregator copy of a job we already hold.

Two things make this design unusual:

- **Descriptions are fetched *before* the gates, not after.** A job whose *title* matched nothing
  can still be admitted on what its **description** says, and fetching first means a JD-rescued row
  takes exactly the same path as a title match rather than a special one. This is "holistic
  matching", and it is the reason the corpus is not limited to whatever vocabulary the title filter
  happened to know about.
- **Every gate's drop counter is printed at the end of the run**, using the same strings the
  architecture diagram is generated from. When a job you expected is missing, you read the run
  output, find the count that is higher than expected, and the line number is right there.

The breadcrumb (`last_new_jobs.json`) is written **before** the database call, so a database hiccup
costs you the insert but not the scrape.

Three further passes run after intake: **scoring** (`score_jobs.py`), **real posting dates**
(`verify_dates.py`, against an external API at ~54 requests/minute), and **repost clustering**
(`reposts.py`).

### 4.2 Scoring: what the number means

`core.score_against(resume_text, analyzed_jd)` answers one question: **how much of what this
posting emphasises does this résumé contain?**

- Scored over **core terms** only — the terms the analyser judged load-bearing. An all-terms version
  could not tell a great match from an average one.
- **Whole-word matching**, so coverage is not inflated by terms that merely sit inside unrelated
  résumé words.
- **Floored, never rounded** — 99.6% stays 99. A partial match can never round up to a misleading
  100, and a real 100 requires a clean sweep of the whole JD, not just of the core terms.
- **A confidence cap.** A posting we could only extract a few keywords from cannot support a strong
  claim about anybody: a "Senior Delivery Manager" whose analysis yielded *one* term read 100%
  because the résumé happened to hold that term, and 9% of the corpus was being judged on three
  terms or fewer. The ceiling now rises with how much of the role we could actually read — one term
  tops out at 16, three at 50, six or more is uncapped. A thin posting can still rank; it just
  cannot claim to be a strong match.
- Term weights come from an **IDF table built over every description in the corpus** — `idf.json`,
  29.4 MB, **1,015,658 terms** (counted by the live `/warm` response). It is **source-of-truth, not
  a cache**: a partial rebuild silently re-weights every score in the corpus.

**A score is STORED, and `resume_fp` is why that is safe.** A stored score is a claim about a
*specific* résumé, and unlike a cache keyed on that résumé's hash a plain table cannot notice when
it stops being true. So the md5 of the profile is stored **with** the score and **every read filters
on it** — a row scored against an older résumé simply does not come back, and the reader recomputes.
The table may be out of date; it cannot serve a number computed against something else.

### 4.3 The filter triplet

"Does this job match this search" is implemented **three times**, and all three must agree:

| Where | What |
|---|---|
| `web.py::_filter_rows` | the server feed |
| `static/app.js::matches()` | the client feed |
| `core.py::prefs_match` | the email digest |

The server/client switch is `_FEED_INLINE_MAX = 4000`: below it the browser gets the whole corpus
and filtering is instant with no round trip; above it that payload is too large, so the server
filters and pages. The consequence is that **the feed silently changes which implementation it uses
as the corpus grows** — so a divergence is invisible until the corpus crosses 4,000, and then
affects everything.

`scripts/feed_parity.py` is the only thing preventing that. It lifts the pure functions out of
`static/app.js` **as source text** and runs them in node, so it tests what actually ships rather
than a Python re-implementation of it.

### 4.4 The read path: the caches, and why each exists

A feed render touches five caches, and the difference between hitting them and missing them is
11 ms against 2.2 s.

| Layer | Scope | Bound |
|---|---|---|
| `_jobs_cache` | process | 3600 s TTL |
| `jobs_snapshot.json.gz` | **file — shared by every worker** | revalidated against `db.jobs_fingerprint()`, max 24 h |
| `score_cache/<sha256>.json.gz` | **file — shared** | 64 files, oldest mtime evicted |
| `row_cache/*.rows.gz` | **file — shared** | 3 files |
| `_base_rows_cache` | process, **exactly one entry** | replaced, never appended |
| `_rows_cache` / `_score_cache` | process, LRU | `_cache_max()` |

**A file is the only cache several short-lived processes can share.** Passenger runs a worker pool
and recycles it freely, so the on-disk layers are what stop each new worker paying full price — and
why a keep-warm ping alone never fixed the cold feed. Measured: after warming four workers in
parallel, three of the next eight probes *still* hit a cold worker at 6.3–7.0 s. A cron cannot win
that race; a shared file removes it.

**Only `score` is per-user.** `_build_row` emits **41 keys** and exactly one depends on who is
asking, so `_base_rows()` builds the other 40 once per corpus and `ranked_rows` overlays the score
onto shallow copies — 1,941 ms → 198 ms per user, byte-identical over all 21,960 rows.

**The rows are built incrementally, and reuse is by value, not by URL.** That is a correctness
argument rather than an optimisation: a scrape does not only add rows, it flips `is_active` when a
posting closes and fills `posted_verified`. Reusing a built row because its URL looked familiar
would show a closed job as open. Comparing the source dict costs 28 ms over the whole corpus and
short-circuits on the first differing key.

---

## 5. Technology — what we use, and how

### Runtime

| | |
|---|---|
| **Flask** (Python 3) | `web.py` — 88 routes, no blueprints, one module. Read the four request hooks first: CSP nonce, security headers, gzip, auto page-view. All four run on every request. |
| **Passenger / LiteSpeed** on shared cPanel | `passenger_wsgi.py`. Two `lswsgi` workers, recycled freely, under a ~1.2 GB CloudLinux LVE memory cap that is invisible from inside the account. |
| **Jinja2** | 33 templates, server-rendered. |
| **Vanilla JS** (`static/app.js`) | The client feed — filtering, card rendering, paging. Twin of the server's implementation. |
| **Vite + TypeScript** (`web/`) | React islands for the pieces that needed real state. Builds into `static/dist/`, committed because the server runs no build step. |
| **Python 3.9** on the box, 3.13 locally | Measured: 3.9 is **1.55× slower**. |

### Storage

**PostgreSQL**, behind one PostgREST-shaped interface (`db.py`) with **three live transports**,
resolved lazily at first use:

| Transport | When | How |
|---|---|---|
| `pgrest.py` | `PG_DSN` is set | Direct psycopg. Reimplements PostgREST's verbs, so the same call works locally and on the box. Loopback-only — this is the cPanel app. |
| `dbproxy.py` | `DB_PROXY_URL` + `DB_PROXY_SECRET` | **HMAC-signed HTTPS.** The only way off-host code — GitHub Actions, this laptop — reaches the database. |
| local CSV | neither | Vestigial fallback. |

A **half-set** `DB_PROXY_*` pair raises rather than falling through, and so does asking for a
session with nothing configured at all. `DB_REQUIRE` turns "wrote to the wrong place and exited 0"
into a loud failure — historically the most confusing class of bug in this project.

**Schema, after the 2026-09-07 revamp:** five tables where there was one. `jobs` holds what the
employer stated; `job_descriptions` holds the text; `job_terms` holds the packed analysis;
`job_facts` holds what we derived; `user_scores` holds per-(user, job) match numbers. The revamp took
`jobs` **316 MB → 27 MB** and the whole database **740 MB → 437 MB**, and stopped the packed analysis
being 38.7 MB resident in every worker.

**Both have grown back since, and the live figures are in §7.** Those are the *outcome* of the
revamp on 2026-09-07, not the current size.

### The scrape

| | |
|---|---|
| `requests` + `BeautifulSoup` | 40 ATS adapters. Most ATS platforms expose a JSON API; the rest are parsed. |
| Playwright | Only where a board is genuinely client-rendered. |
| `python-jobspy` | **Discovery only** — LinkedIn, Indeed, jobright. We keep the employer *name* and throw the posting text away, then scrape that employer's own board. This is what keeps "don't reach for an aggregator" intact: an aggregator is a discovery *channel*, never a feed source. |
| `flock` | Runs skip rather than stack. |
| `SCRAPE_SLICE` | Bounds every accumulator, because being SIGKILLed mid-loop is routine on shared hosting. |

### Scheduling — four slots, two runners

| When (ET) | Runner | What it does |
|---|---|---|
| **~08:00–09:00** Mon–Fri | GitHub Actions (`scrape.yml`, at the repo root) | The heavy pass: sweep, **full** score, `verify_dates`, analytics rollup, repost detection, digest email |
| **13:00** Mon–Fri | cPanel cron (`bin/cron_scrape.sh`) | Sweep, **new-only** score, reposts, user scores |
| **16:00** Mon–Fri | cPanel cron | Same |
| **:30, hourly** Mon–Fri | cPanel cron `--analyze-only` | Writes `jd_terms`, then refills the stored user scores |
| **every 5 min** | cPanel cron | `/warm` × 4, behind a `flock` guard |

Nothing runs at the weekend: employers don't post then, and Actions minutes are capped.

Three sidecar workflows: `scrape-watchdog.yml` (hourly — dispatches the scrape if the scheduled
event never arrived), `jobspy-sweep.yml` (Tue + Thu), `jobspy-shadow.yml` (on demand).

**A cron is not a guarantee, and that is measured, not theoretical.** `scrape.yml` asks for
`47 12 * * 1-5`; the first fire after the requested slot has run +43 min, +9h42, +6h19, +3h56 — and
on 2026-09-04 it never fired at all, while push-triggered runs on the same repository started
normally that same morning. That is the entire reason the watchdog exists.

**`/warm` is the other half.** A restart empties every per-process cache, and a deploy is not the
only thing that causes one — `stderr.log` on the box is a list of `killed by signal: 9` as workers
hit the account memory cap. Measured 2026-09-06: a worker started at 00:49:51 UTC served its first
feed render at 00:53:11 in **17,216 ms** against a steady state of 80–170 ms, and nobody had
deployed for three and a half hours. `/warm` stages the corpus, sponsor counts, visa index, logo
manifest, base rows, the IDF table, the live analysis, and **every account's score file** — off
everybody's path. It is gated on a shared secret and 404s without one, because it is seconds of CPU
on a shared host.

It loops four times because **one call warms one worker**: measured, 1 of 8 consecutive calls took
4,111 ms while the other 7 took ~375 ms.

### Deploy

**`git push` deploys nothing.** cPanel runs `.cpanel.yml` only for a repository *hosted on* cPanel,
and this origin is GitHub. The file exists, is correct, and has never once executed — verified
2026-08-09 by pushing a full release and then polling the live site for five minutes.

```bash
python scripts/build_deploy_zip.py
```

That writes a **flat** `stemjobs1_deploy.zip`; then File Manager → Upload → Extract →
`touch tmp/restart.txt` → hit `/warm` before opening the feed. The builder **refuses to build** if
its file list and `.cpanel.yml` disagree about a module `web.py` imports — the only thing keeping
the two lists in step.

### Security

- **CSP with a per-request nonce**, `X-Frame-Options: DENY`, HSTS, `Referrer-Policy`,
  `Permissions-Policy`.
- **`img-src 'self' data:` and `font-src 'self'`** — and those are not decoration. Every logo is
  harvested and committed to `static/logos/`; all six woff2 files are ours. Nothing is fetched from
  a third party at request time, and the CSP is what *enforces* it. What this replaced was a
  render-blocking stylesheet on `fonts.googleapis.com` pointing at a second host for the binaries:
  two third-party handshakes in front of first paint.
- **CSRF tokens** on state-changing routes, plus `SameSite=Lax` and `form-action 'self'`.
- **HttpOnly session cookies**, salted password hashes, admin-created accounts only.
- **`APP_SECRET` is required whenever a remote database is configured** — `web.py` refuses to import
  rather than sign cookies and extension tokens with a guessable machine-local fallback.
- **Rate limits** on `/api/ext/*` and `/api/feed`.
- Sealed, HMAC-authenticated extension bearer tokens.

### Testing and documentation

```bash
python scripts/run_tests.py --changed
```

**71 suites, all plain scripts** — `python test_title_filter.py` works. Six are `--db`-gated. CI
runs `python-tests.yml` and `web-build.yml`.

`docs/INDEX.md`, `docs/MAP.md` and the four architecture diagrams are **generated from the source**,
and CI fails if they drift. This is not stylistic: the architecture doc before 2026-08-20 described
a Streamlit app reading `jobs.csv` with "no database" and 26 boards, and had been wrong for three
months. A line number in a generated doc is correct by construction rather than by anyone's
diligence.

---

## 6. The data assets

Committed **source-of-truth**, not caches. The server runs no build step, so whatever is in the repo
is what production uses.

| File | Size | What | Provenance |
|---|---|---|---|
| `idf.json` | 29.4 MB | Term weights over every description — 1,015,658 terms | Written by the **full** scoring pass only |
| `idf.json.idx` | 35.1 MB | The mmap'd open-addressed hash table the app actually reads. **Not committed** — built on the box, and larger than the JSON it indexes | Built by `/warm`, rebuilt when stale |
| `sponsor_counts.json` | 3.3 MB | 129,661 employer keys, FY2021–2025 | USCIS H-1B Employer Data Hub |
| `visa_tags.json` | 2.9 MB | 123,473 keys; a bitfield over h1b / h1b1 / e3 / stem_opt / green_card | DOL LCA + PERM (FY2026 Q3) + the E-Verify employer list |
| `norms.json` | 0.8 MB | Role norms + per-employer tool leanings, over 48,339 postings | Built from `job_terms` after a scoring pass |
| `companies.json` | 0.3 MB | 5,337 employers, 14 sectors — a **required** runtime asset | SOURCES + the `boards` table + `sponsors.txt` + corpus spellings |
| `static/logos/` | 2,030 logos | Self-hosted brand marks, 1,039 aliases | Wikidata P154 + each employer's own site icon |
| `sponsors.txt` | 644 names | Hand-curated sponsor list | Hand-edited, deliberately |

**Two of these carry a warning worth repeating.**

`idf.json` — a partial rebuild silently re-weights every match score in the corpus. Don't interrupt
one and commit the result.

The **logos** are judged on their **pixels**, not their status code. The chain this replaced asked a
favicon service that answers HTTP 200 even when it has to *invent* the icon: **53% of tiles were not
a usable brand logo.** The harvester is sequential on purpose and has no `--workers` flag — 12
threads measured 84% MISS against 96% paced, and a throttled fetch gets recorded as a verdict.

---

## 7. The deployed side, verified on the box

*Read over SSH on **2026-09-13 03:48–03:52 UTC**. Everything in this section was measured on the
server itself, not inferred from the repo or from `OPERATIONS.md`.*

### What is actually running

| | |
|---|---|
| Host | `s15175.bom1.stableserver.net` — **bom1 = Mumbai**, 32 cores, x86_64 |
| Interpreter | **Python 3.9.23** (`~/virtualenv/stemjobs/3.9/`) |
| App directory | `~/stemjobs`, **181 MB**; filesystem 95% used, 202 G free |
| Deployed bundle | `deploy_manifest.json` `built_at` **2026-09-12T01:30:23Z**, 2,106 entries |
| Files actually on disk | **2,198** — so **92 stale extras**, which is expected: extraction cannot delete |
| Workers | **two `lswsgi`** — one at **574 MB** RSS (2 h 25 m up), the master at 29 MB (9 h 47 m) |
| `/login` | **200 in 0.71 / 0.75 / 0.80 s**, three consecutive |
| `/static/app.js` | 200, **137,669 bytes** — byte-identical to the file on disk |

### The deployed commit

The manifest carries no SHA, so it has to be established by hashing. `web.py` and `core.py` on the
box match **`5875fd0`** (committed 2026-09-11 21:29 ET — one minute before the manifest's
`built_at`). `companies.json` matches **`579d51c`**, which is newer.

That looks like drift and is not: **`579d51c` changed `companies.json` and nothing else**, so
shipping that one file is a complete deployment of it. **The box is functionally at `HEAD`.**

### Environment (names only — no values read into this document)

`PG_DSN` · `APP_SECRET` · `DB_PROXY_SECRET` · `GH_TOKEN` · `WARM_TOKEN` · `ADMIN_USERS` ·
`SESSION_COOKIE_SECURE`

No `SMTP_*` on the box, which is correct — the digest runs in Actions. **`WARM_TOKEN` is set**,
closing an item `docs/SESSION_HANDOFF_PROMPT.md` still lists as outstanding.

### The crontab, read live

All three slots are present and match `OPERATIONS.md` exactly — including the hourly
`--analyze-only` line that the handoff notes record as documented-but-never-installed. It is
installed.

```
0 17,20 * * 1-5   bin/cron_scrape.sh
30 *     * * 1-5  bin/cron_scrape.sh --analyze-only
*/5 *    * * *    flock -n tmp/cron_scrape.lock true && 4 × curl /warm
```

The cron log's last entry is 2026-09-11 23:31 UTC. That is **not** a stalled scheduler — the box
is on Sunday and those lines are weekday-only.

### A real `/warm` cycle, from the log

The last one, 2026-09-11T23:31:09Z, **total 2,793 ms**:

| stage | ms | n |
|---|---|---|
| `jobs` | 863 | 54,229 |
| `base_rows` | 705 | 54,229 (**rebuilt 0**) |
| `idf` | 601 | **1,015,658** terms |
| `users` | 447 | 9 accounts — 5 already warm, 4 with no résumé, **0 computed** |
| `sponsor_counts` | 45 | 129,660 |
| `visa_index` | 40 | 123,472 |
| `live_analysis` | 86 | 1 |
| `logo_manifest` | 2 | 2,030 |

`rebuilt 0` and `computed 0` are the two numbers that say the incremental path and the stored
score files are both working.

### The database, as Postgres describes itself

`db_stats()` runs server-side with full catalog access, so it sees past the proxy's table
allowlist. **623 MB across 24 tables** (2026-09-13 03:50 UTC):

| Table | Total | Of which index |
|---|---|---|
| `job_descriptions` | **257 MB** | 22 MB (229 MB is TOAST) |
| `user_scores` | **197 MB** | **165 MB** — 4× the 42 MB of data |
| `job_terms` | 79 MB | 26 MB |
| `jobs` | 40 MB | 21 MB |
| `job_facts` | 28 MB | 15 MB |
| everything else | < 6 MB each | |

Two things worth saying plainly. **The database is 623 MB, not the 437 MB the revamp left it at** —
it has grown 186 MB in six days, and `job_descriptions` is most of it. And **`user_scores` is
mostly index**: 165 MB of index against 42 MB of rows, which is the price of the `resume_fp` filter
being on every read.

### Still happening

`stderr.log` holds **23 `killed by signal: 9`** entries, the file last written 2026-09-13 01:24
UTC. Passenger workers are still hitting the account memory cap and being replaced. This is the
condition `/warm` exists to paper over, and it has not gone away.

---

## 8. The things that are actually unusual

Most of this list exists because the obvious alternative was tried first, and measured.

1. **Descriptions are fetched before the filter runs**, so a job can be admitted on what it *says*
   rather than on what it is *called*.
2. **A stored score carries the fingerprint of the résumé it was computed against**, so the table
   can be stale but can never lie.
3. **The same filter logic exists three times, and a test runs the real JavaScript in node** to
   prove they agree — rather than testing a Python re-implementation of what ships.
4. **Generated documentation with a CI gate**, because hand-maintained architecture docs were wrong
   for three months without anyone noticing.
5. **A cache key that hashes file *content*, memoised on the stat.** Keyed on mtime, a deploy — a
   zip extract, so every file rewritten and not a byte changed — invalidated all 40,000 rows and
   charged the first visitor ~7 s.
6. **Nothing that can fail silently may be in a cache key.** It once included two KV maps read over
   the network whose readers swallow a failure into `{}`; workers computed different keys, each
   rebuilt for 7 s, and each overwrote the other's file. Production showed 63 ms and 8,401 ms in the
   same second.
7. **A request never persists the row cache unless its build was full.** The gzip is most of the
   cost, and charging it to whoever loads the feed next is the regression this replaced.
8. **The IDF table is read as an on-disk lookup, not a dict.** `load_idf` built a 1-million-entry
   dict in every worker: 582 ms and 261 MB. As a lookup table: **38 ms, 17 MB.**
9. **A browser extension is part of the data pipeline**, not just a convenience — it is the only way
   past an Akamai bot-wall, because the user's own browser has already passed it.
10. **The aggregator is a discovery channel whose postings we throw away.** We keep only the
    employer name, then go and read their real board.
11. **Board adoption is measured, not automated.** The automatic yield check auto-rejects only at
    zero survivors, so Domino's passed at 1 of 1,000 sampled against 24,663 postings. Concentration
    beats ratio: Tapestry kept 1.1% and every survivor was a distinct HQ role (keep); EoS Fitness
    kept 7.6% and 99% were one repeated store title (reject). A human reads the titles.
12. **Every rejection is recorded once, with its measured reason** — `db.add_blocked(name, reason,
    added_by)`, 38 entries — and `--added-by` tags every adoption batch, so a rollback is one
    statement.

---

## 9. Timeline

### Phase 1 — it exists (2026-05-31 → 2026-06)

| | |
|---|---|
| **2026-05-31** | Initial commit: scraper, scorer, Streamlit app, daily scrape workflow |
| **2026-06-01** | 175 DOL H-1B/GC sponsors + the first Sponsor Careers view; the first 20 probe-confirmed ATS boards; 440 jobs over a 23.5k-term corpus |
| **2026-06-15** | First speed/UX pass |
| **2026-06-16** | Wide net: **2.7k → 13.2k jobs**, a scalable paged feed, and real posting dates via an external verification API |
| **2026-06-17** | Board expansion begins in earnest — SOURCES → 317 |
| **2026-06-19/22** | Chrome extension: multi-step form advance, honest submit verification, passive answer capture |

### Phase 2 — it survives contact (2026-07)

| | |
|---|---|
| **~2026-07-21** | The original cPanel account is **suspended**. The app moves to `stemjobs1` under a different user. |
| **2026-07-26** | Actions cut from hourly to 4×/day after hitting the free minutes cap |
| **2026-07-28** | A Tinder-style swipe deck is built — never wired to a route, and it is not in the live app |
| **2026-07-29** | Fit filters: location / pay / freshness, USCIS sponsor tiers switched on, closed-posting detection, visa timeline, saved search + per-user email digest |

### Phase 3 — it becomes a product (2026-08)

| | |
|---|---|
| **2026-08-01** | Title filter opens to software / data / AI roles: **+8,094 jobs → 25.3k**. Role-track (dev vs management) filter. Two-column feed with a sticky filter rail. |
| **2026-08-07/12** | Extension re-synced after two months of app changes; adapters for the three biggest ATS; vanity hosts recognised by fingerprint; shadow DOM pierced |
| **2026-08-08** | Admin console; egress budget measured |
| **2026-08-09** | Two analytics defects that had inflated every usage number are fixed; the title filter is re-judged by measurement |
| **2026-08-10** | The "Ink & Route" design system — a real token system |
| **2026-08-15** | **Off Supabase onto cPanel Postgres.** Three transports behind one `db.py`. |
| **2026-08-16** | **Adzuna removed** — 6% of the feed, 38% of the JD backlog. E-Verify+ ingestion pipeline lands. |
| **2026-08-18** | Score recalibration; a per-host thin-JD retry ledger |
| **2026-08-19** | `/resume` offline grader ships; the Resume Brain review panel lands |
| **2026-08-20** | **Holistic matching** — admit on description, not just title. Generated, CI-gated docs. |
| **2026-08-21** | The feed scores against one **live** résumé |
| **2026-08-22** | `/companies` directory replaces the Sponsors page; `careers_us.md` becomes a build input, not a shipped asset |
| **2026-08-24** | Sweep persistence — a killed sweep used to bank nothing |
| **2026-08-30/31** | Company discovery pipeline across LinkedIn + Indeed (~6% employer overlap, so near-disjoint); **one hue, one chip**; sponsorship data refreshed on the five-year window |

### Phase 4 — it gets fast, and honest (2026-09)

| | |
|---|---|
| **2026-09-01** | The Supabase transport is deleted — **it had been the silent default.** Base rows built once per corpus (1,941 → 198 ms/user); `row_cache/` on disk; cold path 3,231 → 568 ms |
| **2026-09-02** | JD reading repaired: phantom terms 89% → **0.3%**, junk 17% → 2%. Corpus norms ship. |
| **2026-09-03** | The experience filter reads years the way employers actually write them; the card gets its facts grid; JobSpy PM ingest (+1,804 jobs, +46 boards) |
| **2026-09-04** | **`user_scores` table** — the match number survives a restart, a new worker, and a different device |
| **2026-09-05** | Canonical JD sections: 62% → **94%** multi-section. `/warm` staging: 2,085 → 433 ms. JD coverage drained to **99.2%**. |
| **2026-09-06** | Capacity measured honestly — memory stopped scaling with users; the real ceilings are ~50 accounts and ~10–30 concurrent |
| **2026-09-07** | **DB revamp complete** — five tables; `jobs` 316 → 27 MB, database 740 → 437 MB. The rc=137 mystery is solved: the account was already full before the sweep started. |
| **2026-09-08** | Workday's CXS API reports `total=2000` for any big tenant **and clamps offset**, so 15 boards were being read as an arbitrary slice (Accenture 85,009 postings; 121,803 hidden overall). Fixed by facet-slicing. |
| **2026-09-09** | Board discovery recall: a name-normalisation bug (`johnsonntrols`) plus a discarded redirect target meant 646 of 771 employers resolved nothing. Fixed → **+10 boards / 17,083 postings.** JobSpy findings go live. |
| **2026-09-10** | The cron OOM root cause found: `board_results` held 518k URL strings, the one accumulator `SCRAPE_SLICE` never bounded. 1.46 GB swept off the box. |
| **2026-09-11** | The IDF table becomes an on-disk lookup: **582 → 38 ms, 261 → 17 MB**, measured on the box |
| **2026-09-12** | The shipped company directory carries the 64 boards adopted that day |

**606 commits in 15 weeks.** The shape of the work — 176 in June, 18 in July, 272 in August, 138 in
September so far — is the shape of a project that got built, then got suspended, then got rebuilt
properly.

---

## 10. Honest limits

- **Scale is measured, and it is small.** ~50 accounts (the warm-user cap and the five-minute warm
  window) and ~10–30 concurrent readers. This runs on shared hosting under a ~1.2 GB per-account
  memory cap.
- **Latency is geography.** The server answers in 2–5 ms; a visitor in the US is paying 271 ms RTT
  to a box in India.
- **~272 postings have no readable description at all** — Tesla returns 403, the rest are
  client-rendered shells. That is *unreachable*, not a backlog.
- **44% of descriptions still render with no bullet list**, because the source HTML has none.
- **GitHub Actions minutes are a live constraint** — September stood at 939 of 2,000 on day 10,
  which is why the aggregator sidecar went from seven days a week to two.
- **Seven employers have no board at all** as a consequence of the Adzuna removal (ASML, Deutsche
  Bank, LTIMindtree, Marlabs, Qualcomm, Renesas, Tradeweb). Each needs its real ATS tenant id.
- **Outcome data is zero.** The résumé rubric's band thresholds come from the rubric, not from
  callbacks. Once enough tracked applications carry a result, the weights can be fitted to real
  outcomes — which is the one thing a standalone résumé grader can never do and this can.

---

## 11. Where to read next

| File | Answers |
|---|---|
| [CLAUDE.md](CLAUDE.md) | What must I know before touching anything? |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How does this work? Where do I start reading? |
| [docs/INDEX.md](docs/INDEX.md) | X is broken — which file? *(generated)* |
| [docs/MAP.md](docs/MAP.md) | Where is `_persist_derived`? *(generated)* |
| [docs/map.html](docs/map.html) | Show me the shape. Four diagrams, offline, in colour. |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | How do I deploy / what env var / what runs at 13:00? |
| [extension/README.md](extension/README.md) | The Chrome extension, install to limits |
