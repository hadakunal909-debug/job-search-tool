@echo off
REM refresh.bat - double-click to refresh jobs by hand (no GitHub Actions needed).
REM It just launches refresh.ps1 next to it, which runs scrape -> score -> verify-dates.
REM Run this from the checkout that has your credentials (JobMatch Scraper\.streamlit\secrets.toml).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0refresh.ps1"
echo.
pause
