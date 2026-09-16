# Local performance pass — 2026-09-15

This pass removes repeated work while preserving tested results. The three runtime changes were deployed to stemjobs1.astrochakra.co at 2026-09-16 01:47 UTC (September 15, 9:47 PM EDT).

## Changes

- Feed search applies narrowing controls before fuzzy matching. Searches without narrowing controls keep the early search path.
- Filter-relaxation suggestions count matches without allocating, ranking, or rearranging a result list. They use the same filter predicates as the displayed feed.
- Refresh scoring uses the existing `core.score_pct` equivalent instead of building keyword lists and throwing them away. Source coverage, retry behavior, timeouts, and scoring rules are unchanged.
- Resume Brain reuses sentence extraction within one analysis. No AI model, prompt, context, or generation setting changed.

## Measurements

Windows / Python 3.14.5; 54,229 locally cached card rows with synthetic scores and empty user statuses. Percentage-scoring benchmark uses 5,000 stored analyses and a synthetic resume. Resume analysis uses a synthetic description and an empty IDF map. These measure processing stages, not complete page loads, scrape runs, or AI requests.

Warm-cache medians, alternating before/after execution order: 15 repetitions per variant, 7 for suggestions, 31 for resume analysis. Every pair first requires equal output, including result ordering. Outbound sockets and analytics were disabled. Baseline: `4445ea6a142185aa27fed8f305192781dd937e71`.

| Processing stage | Before (ms) | After (ms) | Time reduction |
|---|---:|---:|---:|
| feed default | 234.32 | 197.27 | 15.8% |
| search without extra filters | 141.12 | 144.64 | -2.5% |
| no-match search | 124.32 | 123.71 | 0.5% |
| filtered fuzzy search | 162.96 | 31.30 | 80.8% |
| filtered newest search | 203.16 | 73.08 | 64.0% |
| filter relaxation suggestions | 466.19 | 137.79 | 70.4% |
| refresh percentage scoring (5000 jobs) | 184.51 | 101.13 | 45.2% |
| resume analysis (synthetic description) | 2.03 | 1.40 | 31.3% |

Controls such as default feed and unrestricted searches varied between runs; do not treat their small differences or the default-feed improvement as an established gain. In the preceding 15-repeat run, the default feed was 176.86 → 175.44 ms and unrestricted search was 121.99 → 121.96 ms. The larger filtered-search and count-only gains persisted across runs. No cold-start or production latency improvement is claimed.

External employer responses and AI generation remain outside these measurements. Resume preprocessing saves less than a millisecond in this example; it does not make the entire AI tailoring request 31% faster.

## Validation

- `python scripts/run_tests.py --changed -j 3`: 58/58 suites passed, including full snapshot score equivalence and server/browser filter parity.
- After the final search-order adjustment: all three feed suites and the new speed-work suite passed again.
- New regression coverage compares counts with displayed results across 672 combinations, verifies rejected rows avoid fuzzy search, and checks every field of resume analysis for unchanged output.
- Documentation was regenerated; `python scripts/build_docs.py --check` and `git diff --check` passed.

## Repeat the measurement

Run from the app directory with the local job snapshot and row cache present. The benchmark fetches no jobs and changes neither cache.

```powershell
python scripts/benchmark_speed_work.py --baseline-ref 4445ea6a142185aa27fed8f305192781dd937e71 --output "$env:TEMP/jobmatch-speed-results.json"
```

## Production release

- Deployed only `web.py`, `resume_brain/analyze.py`, and `scraper/score_jobs.py`; the existing local `idf.json` modification was excluded.
- Before deployment, each live file matched the pre-change git source. The installer checked SHA-256 before and after replacement and compiled each file with the host's Python 3.9.23.
- Rollback copies are under `/home/astrocha/stemjobs/tmp/speed-deploy-20260916T014642Z/before/`; the patch archive and hash manifest are saved alongside them.
- Requested Passenger restart via `tmp/restart.txt`. Live `/healthz` and `/login` returned 200; unauthenticated `/api/feed?limit=1` returned the expected 401 JSON response.
- The manual combined verification/warm command was rejected by automatic approval review with “blocked by policy.” Public verification was completed separately. The existing five-minute cache-warming schedule remains enabled.
