# Job categories and full descriptions

Categories describe the work in a posting, independently of role family and seniority.
The order is actual JD duties, then a specific title, then known company background.
Construction project delivery belongs to Construction & Built Environment. Software
delivery at a construction employer belongs to IT & Software. Company-only and
title-only results are marked Inferred; ambiguous evidence stays Other / Unclear.

`job_categories.py` owns the vocabulary, evidence, source, confidence and rule version.
`job_categories` stores one decision per URL, bound to the full JD hash, title, company,
known company sector and version. Intake, JD replacement, metadata edits and scoring
refresh it. The feed, browser filter, saved preferences and digest use the same value.
Cards show the category; the detail page explains its source and matched duty phrases.

Descriptions are stored and fingerprinted in full. `fetch_jd` has no default text cap;
structured JobPosting records are matched to the requested URL and ambiguous multi-job
pages are rejected. The detail page reports missing, unusable, suspected legacy clipping,
or readable text. Readable is not a guarantee that an employer exposed its full source.

For an existing deployment:

1. Apply `MIGRATION_job_categories.sql` (additive, safe to rerun).
2. Deploy the application and the two operational scripts listed below.
3. Apply `MIGRATION_jd_full_text.sql` to correct old prefix hashes without declaring
   existing derived facts fresh.
4. Run `python scripts/backfill_job_categories.py --apply --report category-applied.json`.
   The default is a dry run. `--verify` fails on any mismatched classification.
5. Run `python scripts/repair_clipped_jds.py --cache-only` to review recoverable cached
   copies, then `--apply` to restore them. Omit `--cache-only` to try the source.
   Limits, checkpoints and retry delays bound each pass; changed source text requiring
   review is never blindly substituted. Successful writes rederive facts and terms,
   invalidate stored match scores and verify the stored full text.
6. Warm the app after the backfill using the existing operator procedure.

Regression coverage: `test_job_categories.py`, `scripts/test_category_storage.py`,
`scripts/test_category_feed.py`, `scripts/test_html_to_text.py`, `scripts/feed_parity.py`,
and the existing JD persistence/rendering suites.
