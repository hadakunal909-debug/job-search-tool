# Scrape recovery — September 15, 2026

## Result

**58 additional employer sources registered; 231 new jobs imported and scored.**
The new jobs all have stored descriptions. The sources are part of `scraper.SOURCES`,
so scheduled runs on cPanel and GitHub use them without a separate discovery run.

| Public platform | Employers tested | Working boards | Jobs listed |
|---|---:|---:|---:|
| ADP Workforce Now | 27 | 27 | 640 |
| Paycom | 29 | 29 | 2,010 |
| TriNet Hire | 3 | 2 | 26 |
| **Total** | **59** | **58** | **2,676** |

Of the 58 working boards, 57 currently list jobs. BEKO Technologies has a valid,
empty board. Islamic Relief USA's saved TriNet board returns HTTP 404 and was not
registered. The generic saved name “YMCA” was narrowed to **Woodson YMCA**, the
employer identified by the public portal. AWIP is displayed as **All Weather
Insulated Panels**. No approved source duplicated a configured board URL or
normalized employer name in the live board list and built-in sources checked.

The 2,676 listings include all roles and locations. Existing intake rules still
apply: 251 passed the initial US/title check; the normal pipeline then applied
the résumé terms, deduplication, dates and other existing rules, importing 231.
It reused the saved listings and fetched only 179 missing descriptions; all 179
succeeded. It scored only the 231 imported jobs. No full-universe scrape ran.

Per-employer URLs, prior status, counts and adoption decisions:
[recovery evidence CSV](data/recovered_boards_2026-09-15.csv).

## What changed

- **ADP:** read the public listing and per-posting APIs instead of the JavaScript
  career-page shell. Preserve both employer `cid` and career-center `ccId`.
  Use the public external posting ID, not the internal requisition ID.
  A reproduced cookie collision caused HTTP 500 when moving between employers;
  public API requests now omit that shared cookie. Employer ownership during
  closed-job reconciliation uses tenant and center IDs, not the shared path.
- **Paycom:** the public career page supplies an anonymous visitor context for
  its own listing API. Use it only at the known Paycom service host, keep it in
  memory briefly, and retrieve full descriptions separately. Listing previews
  are never stored as complete descriptions. No applicant login is involved.
- **TriNet:** normalize saved posting URLs to the employer's `/jobs` table.
  Read the server-rendered listing and description block. An absent table or
  unexpected pagination is an error, not an empty result.
- All three platforms are registered in detection, probing and scraping;
  description enrichment is wired into the existing scorer. Pagination failures
  preserve partial results and do not claim a complete successful scrape.
- The company directory was regenerated from the live database and source list.

## What the remaining numbers mean

The starting audit contained **2,068** employers: 1,176 unresolved, 703 timeouts,
and 189 fetch errors. This pass recovered 30, 27 and 1 from those groups,
respectively. **2,010 remain without a verified working board in this recovery
audit:** 1,146 unresolved, 676 timeout-labelled and 188 fetch-error-labelled.
These are retained audit labels, not new live failure tests of every employer.

Offline triage of the original 2,068 found:

| Saved evidence | Employers | Best next step |
|---|---:|---|
| Aggregator links only | 1,315 | Identify the employer's official domain and career page first |
| Employer career page available | 467 | Follow redirects and public ATS links; reuse shared adapters |
| Direct employer posting available | 245 | Recover the tenant/board from the posting URL |
| Name already configured | 36 | Check aliases and existing board health before adding a source |
| Prior board available | 5 | Test the known endpoint directly |

The old timeout bucket often records a short discovery budget being exhausted,
not a job site refusing a request. Similarly, an HTTP error on a marketing page
does not establish that the underlying ATS is unreadable.

**There is no evidence that all 2,010 are impossible to scrape.** The efficient
next pass is domain identification for the 1,315 aggregator-only records and
small platform batches from the saved direct links. Blind employer-name slug
guessing is poor use of the request budget.

Specific limits observed in the exploratory checks:

- Dayforce's public page loaded, but the tested job-search API request returned
  403. A normal public session/API integration still needs validation; this
  single response does not establish that all Dayforce employers are blocked.
- Islamic Relief USA's old TriNet endpoint is gone (404). Find its current
  employer-linked portal rather than retrying that URL.
- `recruiting.adp.com` and `myjobs.adp.com` use different products. The Workforce
  Now adapter does not claim support for them.
- A readable page with zero vacancies is valid coverage; it cannot yield jobs
  that the employer is not currently publishing.

## Verification and delivery

- 37 affected test suites passed; the final adapter-specific regression run
  passed 16 tests, including pipeline return values, public IDs, tenant isolation,
  missing-schema errors, partial pagination and description extraction.
- Deployment bundle validation passed. The six changed runtime files were
  uploaded to cPanel and read back for equality; Passenger restarted and warm
  returned HTTP 200.
- Original retry snapshots and checkpoints remain intact. The new runner is
  resumable and records results separately under `outputs/recovery_2026-09-15`.
- The directory's global classification check already fails on the expanded
  live corpus: the pre-change asset contained 1,437 Unsorted companies out of
  5,575. The refresh exposes 1,485 of 5,648 and 24 prominent unclassified names.
  Its 5% quality threshold was not weakened. This is a separate directory
  classification backlog, not a failed scrape or lost company.

## Reproduce a bounded platform check

```powershell
python scripts/recover_failed_boards.py --out outputs/recovery_2026-09-15 --adp
python scripts/recover_failed_boards.py --out outputs/recovery_2026-09-15 --paycom
python scripts/recover_failed_boards.py --out outputs/recovery_2026-09-15 --trinethire
```

Existing results are reused. `--retry-errors` retries only failed targets after
an adapter fix. Requests are paced; each employer gets a 75-second request
budget, with no automatic retries. The runner itself writes local evidence,
not production jobs or board registrations.
