# Official career URL discovery — September 15, 2026

## Findings

Searched official employer sites and public employer-hosted postings for **31 employers**
whose saved audit evidence contained only aggregator links. Verified complete job listings
for **20 employers**, containing **5,138 postings** across 21 boards.

**19 employers / 20 boards are newly registered.** Sail Biomedicines already has the same
Greenhouse board in the live registry under its former name, Sendabiosciences, so it was
not added twice. Cox Automotive also matched an existing source by normalized name; its
new career page still needs endpoint investigation. NCR's internship board is currently
empty; its US board lists 170 jobs.

Thirty confirmed career links were added to the directory generator. Nexon America's
search result resolved to an official locale page that rendered a generic home shell;
that lead remains in the evidence file and was not promoted as a verified career link.

[Company-by-company URLs, listing counts, decisions and remaining gaps](data/discovered_careers_2026-09-15.csv).

## Discovery fixes

- Greenhouse script embeds such as AQR's `/embed/job_board/js?for=aqr` were truncated
  to a fictitious `embed` tenant. Match the embed form before generic board paths.
  The corrected AQR board returned 54 jobs.
- Ashby tenant names containing encoded spaces were truncated at `%`, turning
  Tools for Humanity's board into `/Tools`. Preserve encoded characters and dots.
  The corrected board returned 19 jobs.

Both reproduced failures now have regression tests. These fixes apply to future
URL discovery, beyond the employers registered in this batch.

## Bounded approach

Follow official links and employer-hosted search results; do not invent board slugs.
Cache page reads, allow at most 150 seconds per employer, and retrieve at most two
selected boards per company. NCR was scoped to its US and internship boards.
The probe runs, including targeted retries after fixes and new evidence, used 283
HTTP requests; initial page research and description enrichment are separate.

Intake reuses 5,129 saved listings from the newly registered boards, applies the normal
US/location, title, résumé, date and duplicate rules, and scores only newly imported
jobs. No full-universe scrape, pruning or closed-job reconciliation was run.

## Remaining gaps

An error on a marketing site is not proof that jobs cannot be fetched: Tempus and
Venture Global returned errors there, while their Workday feeds supplied 152 and
255 jobs respectively. Other cases still need a concrete integration step:

- Orange Logic and Cosm: validate complete custom-page listings and pagination.
- Suffolk Construction and Nebo: native iCIMS and JazzHR integrations need validation.
- Publicis Health Media: identify the employer filter in the shared group job system.
- Copart, Mathematica, Burns & McDonnell, RELX and Nexon: current endpoint or
  career-page resolution remains incomplete. Cox has an existing source to reconcile.

Twenty entries from the previous 2,010-employer unresolved recovery audit now have
verified feeds, leaving **1,990 without a verified feed in this audit**. This is not
an assertion that 1,990 companies are impossible to scrape, nor a new full-universe
health check. Across both September 15 passes, 77 employer sources were newly added.

## Delivery verification

- **212 new jobs** imported from 17 employers; all 212 have stored descriptions and
  match scores verified by a targeted read from the live database. The other two
  newly registered employers had no additional jobs passing this application's rules.
- Missing descriptions were fetched for 196 kept postings; all 196 succeeded.
  Listings already containing full descriptions were reused. Intake and scoring
  completed in about 274 seconds.
- All 37 affected test suites passed. The company-page suite also passed after
  regenerating the directory and assigning sectors to the newly added employers.
- The directory was rebuilt from 1,324 live board records and 59,447 live corpus
  rows, yielding 5,658 companies. The existing global industry-classification check
  still fails: 1,482 companies are Unsorted (26.2%), including 26 prominent names
  outside this batch. Its threshold was not changed.
- Documentation generation and full deployment-bundle build passed.
- Server file readback, restart and Git synchronization are verified at delivery.
