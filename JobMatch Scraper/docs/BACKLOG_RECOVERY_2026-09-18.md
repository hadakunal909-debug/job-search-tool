# Backlog recovery - September 18, 2026

Registered and read back 327 public boards, including Tata Consultancy Services and 59 university-related boards. All 38 live admin blocks remain unchanged. This release supplies the new adapters and detection/description handlers to the scheduled GitHub and cPanel scrapers.

The updated PDF and companion CSVs are in `outputs/backlog_2026-09-18/`. All 2,975 original employer rows retain their dispositions; unresolved candidates are not approved sources. The PDF lists the 327 added boards and contains 3,438 clickable links.

Validation: 75/75 suites passed in the working checkout and 64/64 changed suites passed in the isolated release. Runtime imports were verified under cPanel Python 3.9. The company-directory build succeeds, but its global Unsorted-sector check remains above 5% (28.7%, previously 28.1%). The 42 prominent-company sector warnings were fixed. Normal ingestion and filtering rules remain in effect.
