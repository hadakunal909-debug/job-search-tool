# JobMatch: QA audit — status register

The defect register from the 2026-08-24 test-engineering passes, with what was done about each
finding. **Ids are permanent**: `S3` stays `S3` after it is fixed, so a later pass can say "still
open" or reopen the same id instead of minting a new one.

**Sibling documents.** [LOAD_SECURITY_QUALITY_REPORT.md](LOAD_SECURITY_QUALITY_REPORT.md) is the
point-in-time *measurement* report. The original audit — full repro steps, evidence blocks and
measured tables for every id below — is the source this was worked from; keep it alongside this
file rather than re-deriving it.

**Status vocabulary:** `Fixed` · `Fixed (unverified)` · `Partially fixed` · `Deferred` · `Open`.

> ### ⚠ Everything marked *(unverified)* has not been executed
> The session that made these changes could not run anything — no interpreter, no test suite, no
> `git`. Every fix below was written and read back, and **none of it was run**. Before this goes
> anywhere near a deploy:
>
> ```bash
> EV_OFF=1 python scripts/run_tests.py
> ```
>
> then `EV_OFF=1 python scripts/feed_parity.py` (the filter triplet was touched) and
> `python scripts/test_contrast.py` (the CSS was touched). Expect to fix things.

---

## Register

| Id | Sev | Area | Status | What changed |
|---|---|---|---|---|
| D1 | **Critical** | Deploy | **Partially fixed** | `build_deploy_zip.py` writes `deploy_manifest.json` (sha256 per shipped `templates/`+`static/` file); `web.py::template_drift()` compares the running tree and reports on `/admin`. Verified end-to-end. **The prod-template pull is still outstanding — do not deploy until it happens.** |
| S1 | High | Ext API | Fixed | `_ext_user` shape-gates, then verifies the epoch-0 HMAC with **no DB read**; the epoch lookup is budgeted per caller. `_account_cache` is now true LRU. Measured: 20 invalid tokens → 5 lookups (was 20); an active user survives a 2,200-entry spray. |
| S2 | High | Ext API | Fixed | Per-namespace limiter buckets, oldest-first eviction; burst tiers on every ext class. Measured: 900 → **30** accepted in one instant, AI class 40 → **3**, feed limiter survives a 5,001-token spray. |
| S3 | High | Scraper | Fixed (unverified) | New `scraper.host_is()`; every unanchored `in host` / bare `endswith` in `detect_board` anchored. `probe_board`'s workday arm routed through `_safe_post`. |
| S4 | Med | Scraper | **Deferred** | DNS-rebinding TOCTOU. The fix is a pinned-IP `HTTPAdapter` — a change to the transport every scraper shares, which must not land unexercised. Residual documented in `public_http_url`'s docstring, including what *is* closed (redirect re-validation; IPv4-mapped, decimal/octal and NAT64 literals). |
| S5 | Med | Web | Fixed (unverified) | `/board/delete` scoped to `added_by == you` (admins exempt), and it reports the outcome instead of `except: pass`. |
| S6 | Med | Web | Fixed (unverified) | `/reload` is POST + admin + CSRF. Feed button is an admin-only form; `app.js` posts with the token. |
| S7 | Med | Web | Fixed (unverified) | `/add` validates through `scraper.is_http_url` before anything is detected, and stores `canonical_url`. |
| S8 | Med | Web | Fixed (unverified) | `db.USER_STATUSES` whitelist enforced inside `set_user_status`, so both routes and the extension inherit it; the routes 400 rather than 500. |
| S9 | Med | Résumé | Fixed (unverified) | Tectonic tarball verified against `TECTONIC_SHA256` before `chmod`; an unpinned version is refused loudly. `/brain/export/resume.pdf` rate-limited. **⚠ The digest table is empty — see "Needs your input" below.** |
| S10 | Low | Web | Fixed (unverified) | `_csv_cell()` prefixes `= + - @ \t \r` in `/applications.csv`. |
| S11 | **High** | Résumé | Fixed (unverified) | The AI key is sealed (HMAC-CTR encrypt-then-MAC, stdlib only) instead of sitting readable in the signed cookie. Old plaintext sessions are re-sealed in place. Copy corrected. |
| F1 | High | Scraper | **Partially fixed** | `_norm_name` deletes apostrophes instead of splitting on them, so `Kohl's` → `kohls`. **The indexes still need rebuilding** — see below. |
| F2 | — | Tests | **Not reproducible** | Baseline on this machine was **47/47**, not 43/45; the audit ran on a different checkout. F1's defect is real and was confirmed live — the suite simply has no case for it. It does now. |
| F3 | Low | Ext API | Fixed (unverified) | `ext_debug` bounds the record before serialising, so the JSONL stays parseable. |
| F4 | Low | Analytics | Fixed (unverified) | `try` moved inside the `/api/ev` loop. |
| F5 | Med | Feed | Fixed (unverified) | `login_required` returns `401 {…}` for `/api/*`; `app.js` reads a 401 the way it reads a 429, on both the reset and Load-more paths. |
| F6 | Med | Feed | Fixed (unverified) | `#popcount` written inside `setCount()`, from one value in one tick. |
| F7 | Med | Feed | **Partially fixed** | `canonical_url` folds the Workday site segment; test cases added. **Existing duplicate rows still need merging** — see below. |
| F8 | Med | Boards | **Partially fixed** | Oracle names now derive from `/sites/<site>` (Staples, not `Fa Exhh Saasfaprod1`). **Existing `boards` rows still need the repair sweep.** |
| F9 | Low | Feed | Fixed (unverified) | `formatDates()` after the in-place card re-render. |
| F10 | Low | Feed | Fixed (unverified) | `tabCount()` updates `.tabn` from the status transition. |
| F11 | **High** | Brain | Fixed (unverified) | `research.is_product_copy()` drops storefront pages from the summary and keyword corpus. **Poisoned cache rows still need invalidating.** |
| F12 | Med | Brain | Fixed (unverified) | `analyze._sentences` falls back to `jdrender.jd_flat_list` when a JD has no punctuation. |
| F13 | Med | Feed | Fixed (unverified) | `applied` drops off the default tab alongside `hidden`, in both twins. `liked` deliberately stays. |
| F14 | **High** | Feed | **Partially fixed** | Cards carry a **"years not stated"** badge while a years filter is on, and a new *Only postings that state their years* toggle (off by default) hides them. **Raising `experience_years` recall — the actual defect — is not done.** |
| F15 | **High** | Brain | Fixed (unverified) | Research asks the verified `company_domains.json` + posting URL first; a guessed domain must corroborate the employer before anything is cached. |
| F16 | Med | Data | **Partially fixed** | Every count now carries its vintage, derived from the data (`sponsor_data_through()`), so "top sponsor" reads "top sponsor to FY2023". **The refresh itself needs new USCIS/DOL files.** |
| F17 | Med | Feed | **Deferred** | Depends on F16's data half. Role-level matching also needs the LCA titles indexed, which they are not. |
| M1 | **High** | Auth | Fixed (unverified) | Change-password form on `/profile`, rate-limited, through `auth.password_problem`/`hash_password`. Forgot-password still needs an email sender; the page says so. |
| M2 | Low | Company | Fixed (unverified) | Filter bar extracted to `_filterbar.html` and included on `/company`, so an employer's roles narrow by role/match/date/experience/pay without leaving the page. |
| P1 | Med | Web | Fixed (unverified) | `save_active_resume` calls `_bust_profile(user)` instead of clearing every user's scores. |
| P2 | Med | DB | Fixed (unverified) | `pgrest.Session` guards `_connect` and `_run` with an `RLock`. |
| P3 | Low | Web | Fixed (unverified) | Key snapshot before filtering; `analytics._start` guarded by a lock. |
| U1 | Low | Résumé | Fixed (unverified) | The refusal names the formats the host can actually read, probed rather than hardcoded. |
| U2 | Med | Global | Fixed (unverified) | `@errorhandler(404)`/`(500)` render `error.html` with the app's chrome; JSON for `/api/*`. |
| U3 | Med | Global | Fixed (unverified) | Flash categories → `.is-error`/`.is-ok`, with `role="alert"`/`"status"`. Uncategorised call sites unchanged. |
| U4 | Low | Login | Fixed (unverified) | The duplicate wrong-password banner dropped; the inline error (which has the ARIA) stays. |
| U5 | Low | Feed | Fixed (unverified) | While `q` is non-empty the match control greys out, reads "Off while searching", and stops counting toward the Filters badge. |
| U6 | Low | Feed | Fixed (unverified) | `core.tidy_location()` at render. |
| U7 | Low | Global | Fixed (unverified) | One `ago` Jinja filter used by `/job` and `/applications`, matching the feed's wording; ISO on hover. |
| U8 | Low | Job page | Fixed (unverified) | The row's own company tokens subtracted, via `core.norm_company` as well as the raw label. |
| U9 | Low | Feed | Fixed (unverified) | Status tabs get their own empty state and a tab switch, not a filter reset that cannot help. |
| U10 | Low | Feed | Fixed (unverified) | "Clear all filters" clears `q` too. |
| U11 | Low | Company | Fixed (unverified) | `margin-right` on `.backlink`. |
| U12 | Low | Brain | Fixed (unverified) | Empty state hides while its creation form is open. |
| U13 | Low | Brain | Fixed (unverified) | No arrow when there is no lift; says the library is empty. |
| U14 | Low | Feed | **Documented** | `max="75"` left as-is — `test_saved_search.py` asserts the attribute string verbatim — and the rationale written down where it was missing. |
| U15 | Med | Job page | Fixed (unverified) | `core.PERK_TERMS` excluded from `_useful_terms` and from `/company`'s skill chips. |
| L1 | Note | — | **Partially fixed** | `Content-Disposition` filename strips control characters. The dbproxy replay window and the BREACH preconditions remain documented-and-accepted. The two `esc()` definitions were **not** renamed. |

---

## Still needs doing

**Before anything else — run the suite.** Nothing above marked *(unverified)* has been executed.

### Needs your input
* **S9 — `TECTONIC_SHA256` is empty.** Auto-download is therefore *refused* on Linux/macOS and
  PDF export falls back to `.docx` until you fill it in. That is the safe direction, but it is a
  behaviour change. Fill the table with digests you verify yourself:
  ```bash
  shasum -a 256 tectonic-0.16.9-x86_64-unknown-linux-musl.tar.gz
  ```
* **D1 — pull `templates/` off the cPanel box** and diff it against the repo. Production carries
  a `#themepick` appearance card that exists in no commit, a `base.html` missing the theme toggle
  from `5bb4dc7`, and a doubled `/welcome` callout. The next zip extract destroys all three.
* **F16 — the H-1B refresh** needs the USCIS Employer Data Hub bulk CSVs for FY2024/25 and the
  DOL disclosure files. Those are downloads; say the word and I'll confirm the files and sizes
  first.

### Data jobs, once the code is verified
* **F1 — rebuild the sponsor indexes.** `sponsor_counts.json` was written by the old rule and
  still carries 1,316 stray-`s` keys, so the index is polluted on its own side until:
  ```bash
  python -m scraper.build_sponsor_counts
  ```
* **F7 — merge the duplicate Workday rows.** `canonical_url` stops new ones; the existing pairs
  (117 Applied Materials openings split across two directory entries) need a one-off sweep,
  preferring the spelling that resolves a logo.
* **F8 — sweep `boards`.** Any row whose `company` misses the sponsor index but whose URL carries
  a resolvable `/sites/<site>` name is a repair candidate.
* **F11 / F15 — invalidate the poisoned research rows**, so the Walmart storefront scrape and any
  wrong-domain records are re-crawled under the new checks.

### Deliberately not done
* **S4** — see the register. A pinned-IP transport adapter must be exercised against real boards.
* **F14 step 1** — raising `experience_years` recall is a measurement job: build a reject dump,
  drive the 18% corpus-wide unknown rate down, and re-measure. The badge and the toggle make the
  current state legible; they do not fix it.
* **F17** — blocked on F16.
* **L1's `esc()` rename** — both definitions are currently used correctly; the shared name is the
  hazard, and renaming one touches every call site in two files.

---

## Guards added

`test_qa_audit_fixes.py` (in `scripts/run_tests.py`'s `SUITES`) covers S1, S2, S3, S8, S10, S11,
F1, F3, F7, F11, F15, U1, U6, U8 and U15. `test_canonical_url.py` gains three Workday
case-variant cases. A finding with no guard comes back — the ones still without one are S5, S6,
S7, S9, F4, F5, F6, F9, F10, F12, F13, M1, M2, P1, P2, P3 and the remaining UI ids.
