# Description readability and experience accuracy

## What changed

- Plain text and Markdown retain their paragraphs and bullets through `html_to_text`.
- Opening and closing HTML blocks both preserve boundaries; the microdata scraper uses the same converter.
- Required Qualifications renders as a section with a working jump link.
- Experience extraction respects required and preferred sections, including older flattened descriptions. Ordinary prose mentioning preferred qualifications does not start a section.
- A minimum inside a preferred section remains preferred unless explicitly marked required. Degree alternatives can continue into an experience-only alternative. Age requirements and year-long contracts do not become experience requirements.
- The job page exposes source excerpts beneath its experience summary. The excerpts come from the same parsing pass used to derive filter years.
- The scoring revision includes experience parsing helpers and their rules, so those edits restart the analysis cursor.
- A database revision now changes when jobs, descriptions, facts, or analyzed terms change. Feed workers check that revision after at most one minute of cache reuse, so correcting an existing posting invalidates its old filter value.
- iCIMS detail extraction rejects the observed placeholder dates generated from the current request time (two years before / one year after). Undated listings omit the date field so intake can label the discovery timestamp as added, rather than pretending it is an employer posting date.

The existing filter policy remains: highest required minimum, otherwise highest preferred minimum; the lowest stated option within a degree ladder. Missing years remain unknown. These rules do not establish that an employer's current page matches a cached description.

## Validation and rollout

Regression coverage lives in `test_experience_years.py`, `scripts/test_html_to_text.py`, and `scripts/test_job_page.py`. Run the renderer and feed parity suites as well. `scripts/audit_jd_reading.py` checks a conservative reading against cached descriptions; its missed-phrase rate is not an overall accuracy score.

### Live rollout: September 15, 2026

The parser, renderer, experience evidence, and cache revision were deployed to the live app. A guarded backfill corrected **1,893** experience values, with no concurrent-write conflicts. A subsequent full audit checked **60,143** stored postings and found **zero** differences between stored experience and the updated parser's reading of the database description.

This establishes consistency with stored descriptions, not that every employer page was freshly verified. Three accessible employer pages (Aquent, Ascension, and Boeing) were checked separately and agreed with the corrected two-year requirements. One attempted Amgen page could not be retrieved. The full audit found 263 missing descriptions and 409 non-posting responses; unavailable text remains unknown. Descriptions whose original formatting or text was lost need to be fetched again to recover it.

Date checking exposed an unreliable source field: Ascension's detail JSON-LD generated a different timestamp on each request, dated exactly two years before the request. Its listing supplied a separate posting date. The new guard rejects that generated detail value; it does not replace a listing date with the apparent two-year-old value. Boeing's stored verified date matched the source date. These checks do not establish date accuracy across the whole corpus.

The release's offline validation passed 69 affected suites, including description rendering, experience extraction, cache invalidation, and server/browser/digest filter parity. Targeted cache and job-page checks were rerun after reducing the revalidation interval.

### Deploying and refreshing existing data

Apply `MIGRATION_job_data_version.sql` on the database host before deploying the updated fingerprint reader. It adds statement-level revision triggers without rewriting posting data. Without this migration, fingerprint reads return unknown and the app must re-read instead of trusting an unchanged cache.

Deploying code alone does not backfill stored experience. For an authoritative audit on the database host, with `PG_DSN` configured:

```bash
EV_OFF=1 python scripts/refresh_experience.py --report outputs/experience-before.jsonl
EV_OFF=1 python scripts/refresh_experience.py --apply --plan outputs/experience-before.jsonl --report outputs/experience-applied.jsonl
EV_OFF=1 python scripts/refresh_experience.py --check --report outputs/experience-verified.jsonl
```

Use a new report filename for each run. The script reads bounded batches of current database descriptions, records old/new values and source excerpts, and guards writes against concurrent description or experience changes. `--plan` limits repair to the prior audit's URLs but still re-reads and recomputes them. Summary JSON files accompany the reports. The regular scoring pass also uses the new parser and restarts its cursor when these extraction rules change.

After a release, invalidate shared job/row caches, restart Passenger, and warm the app as described in `OPERATIONS.md`. Keep the repair report and pre-deployment code backups for rollback.

### Comparing a local cache

For a read-only consistency report:

```powershell
python scripts/audit_experience.py --report outputs/experience_audit.json
```

With the intended remote database configured, compare current stored facts with the local description cache:

```powershell
python scripts/audit_experience.py --live --check --report outputs/experience_audit.json
```

The audit checks description fingerprints before comparing years. Missing fingerprints, missing text, and different cached text are reported separately; `--check` fails for incomplete verification as well as mismatches. Older feed snapshots omit fingerprints and therefore cannot establish consistency. This command writes only its report, never job data, and does not visit live employer pages.
