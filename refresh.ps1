# refresh.ps1 — run the job scraper by hand (no GitHub Actions).
# Runs the same three passes the scheduled Action does:
#   1. scrape        — pull new jobs from every board into the database
#   2. score_jobs    — fetch missing job descriptions + compute the match % (rebuilds idf.json)
#   3. verify_dates  — look up each job's real posting date
#
# Live progress prints to this window AND is appended to "JobMatch Scraper\refresh.log".
# IMPORTANT: run this from the checkout that has your credentials — the folder whose
# "JobMatch Scraper\.streamlit\secrets.toml" holds your Supabase url/key. Without those it
# falls back to a local jobs.csv and will NOT update the live site.

$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath (Join-Path $PSScriptRoot 'JobMatch Scraper')
$log = 'refresh.log'

function Run([string]$label, [string]$module, [string[]]$extra) {
    Write-Host "`n==== $label ====" -ForegroundColor Cyan
    "`n==== $label  ($(Get-Date)) ====" | Out-File -FilePath $log -Append -Encoding utf8
    if ($extra -and $extra.Count) { & python -u -m $module @extra 2>&1 | Tee-Object -FilePath $log -Append }
    else                          { & python -u -m $module        2>&1 | Tee-Object -FilePath $log -Append }
}

"Refresh started $(Get-Date)" | Tee-Object -FilePath $log -Append
Run 'Scrape new jobs'                'scraper'               @()
Run 'Score jobs (match % + JD)'      'scraper.score_jobs'    @()
Run 'Verify posting dates'           'scraper.verify_dates'  @('--workers', '24')
Write-Host "`nAll done. Full log: $((Resolve-Path $log).Path)" -ForegroundColor Green
