# Public platform support — September 15, 2026

## Result

Implemented reusable adapters for **JazzHR**, **native iCIMS** and **Cornerstone**.
Registered **41 additional sources** with **10,728 public listings** before the
application's location, relevance, freshness and duplicate filters. **612 new active jobs**
from 33 employers now have verified stored descriptions and match scores.

| Method | Newly registered sources | Public listings |
|---|---:|---:|
| Native iCIMS HTML | 33 | 9,759 |
| JazzHR hosted career pages | 5 | 265 |
| Cornerstone public career-site API | 1 | 17 |
| Existing Jibe adapter, after locating migrated portals | 2 | 687 |
| **Total** | **41** | **10,728** |

[Source URLs, original employer labels, corrected display names and decisions](data/supported_public_boards_2026-09-15.csv).

## What made these sources readable

### Native iCIMS

The shared scraper advertised an old Chrome browser. On the same Suffolk URL,
that header returned HTTP 405, while the actual Python requests client identifier
returned the complete public search page. The adapter now uses its truthful client
identifier. No applicant login, CAPTCHA solving or verification-token extraction
is used.

Of 37 native iCIMS URLs checked, 34 supplied complete lists. The adapter follows
published page links, checks the current/total page numbers, rejects repeated
pages, and preserves earlier rows if a later page fails. Error/verification pages
cannot become successful empty boards.

Locations can appear in the card header or the extra-fields list. Both are parsed,
and country-state-city codes become readable locations that the state filter can
recognize. Some portals omit locations entirely; up to 50 missing locations on
accepted titles are enriched from posting metadata, with a 45-second budget.
Unknown locations remain unknown when metadata cannot be retrieved. Full descriptions
come from JobPosting structured data or the complete content sections, excluding
application controls and talent-network copy. Card previews are never used as full JDs.

Live intake exposed a further distinction: canonical posting URLs render an iframe
wrapper. The adapter now requests the employer's inner page using `in_iframe=1`.
The intake queue also receives previously missing posting dates and locations before
applying its filters, while preserving explicit metadata already present on the listing.
This behavior has a regression test and was verified on live canonical posting URLs.

### JazzHR

The hosted `/apply` pages contain the public job list. Preserve the case-sensitive
posting ID, title and location. Read only the description container on detail pages,
excluding applicant forms. Pages serving Windows-1252 punctuation are decoded correctly.
Pagination and unexpected/empty schemas are checked explicitly.

### Cornerstone

Mathematica's current site supplies an anonymous visitor context used by its own
search interface. The adapter uses that short-lived context for the public search
API and full posting descriptions. It never persists the token and sends it only
to the matching employer's CSOD host or a validated CSOD API endpoint; description
requests do not follow redirects carrying it.

The site's “Anytime” filter uses `null`, not zero. Zero returned no jobs; reproducing
the actual UI request returned 17. Pagination is verified against the reported total.

All three adapters are wired into source discovery, board probing, scheduled scraping
and description enrichment. Adding another employer on these supported page formats
can reuse the adapter.

## Identity and migrations

- Nebo Agency and Suffolk Construction, previously unsupported in the URL-discovery
  batch, now have working adapters. Mathematica now has a verified Cornerstone feed.
- Astrion and Tower Health's old iCIMS pages contain JavaScript redirects. Their
  current employer domains expose working Jibe feeds (344 and 343 listings).
- Generic or misleading saved labels were corrected using the public portal's
  identity: YMCA of Central New Mexico, UIC Alaska, Quanta Services, Ferrellgas,
  Urban Outfitters, Spencer's and Spirit Halloween, and Lewis Resource Management.
  In particular, the Ferrellgas board does not establish coverage for the old label FNA.
- The DHA portal returned 208 listings but calls itself **DHA (Archive)**. It was not
  registered without evidence that this remains the current employer publication.
- UnityPoint's old endpoint redirects to a new career site whose listing endpoint
  was not validated in this pass. A redirect is not recorded as an empty board.

## Scope and efficiency

The native iCIMS/JazzHR batch tested 42 saved employer URLs, using up to three
independent workers and a 90-second request budget per employer. Targeted rechecks
validated the header, location and date fixes. Import reuses the final saved listings;
it does not rescrape the source universe. Description fetching uses two workers,
and only newly imported rows are scored. Pruning and closed-job reconciliation are disabled
for this import.

This does not claim universal support for every iCIMS/JazzHR/Cornerstone deployment.
Other previously identified platforms and custom sites, including Dayforce, Cosm,
Orange Logic and Burns & McDonnell, still require their own verified integrations.

## Verification and delivery

- 39 affected test suites passed, including 15 new adapter regression tests.
- The new tests cover complete/partial pagination, repeated pages, empty vs error
  states, tenant identity, foreign-location filtering, full descriptions, dates,
  client headers and visitor-token destination/redirect restrictions.
- Initial intake wrote 1,050 rows. The iframe repair retrieved metadata for 934
  native iCIMS postings. Reapplying the normal intake rules retained **612 active jobs**
  and removed 438 entries from this import: 436 outside the age window and two without
  usable descriptions. A checked query found no user-tracked jobs among those 438.
  Existing corpus rows were outside this cleanup's target set.
- A final targeted database read verified **612 active rows, 612 descriptions and
  612 match scores**. Scoring was rerun after repairing metadata; it fetched no further
  descriptions and used the existing IDF index.
- The directory was regenerated from the live database: 60,143 corpus rows and
  1,324 database boards, combined with built-in sources, yielded **5,687 companies**.
  The existing global classification check still fails: 1,480 Unsorted companies
  (26.0%) and 27 prominent unclassified names. Its threshold was not weakened.
- Documentation generation and deployment-bundle build passed. Six runtime files
  were uploaded to cPanel and read back for equality. Passenger restarted and
  the warm request returned HTTP 200. Git synchronization is completed at delivery.

## Recheck the saved sources

```powershell
python scripts/recover_public_career_pages.py --targets docs/data/supported_public_boards_2026-09-15.csv --out outputs/public-platform-recheck
```

This checker writes local evidence only. It reuses its checkpoint on subsequent runs;
`--retry-errors` retries incomplete checks. The evidence CSV also includes the two
Jibe migrations, which this adapter-specific checker skips.
