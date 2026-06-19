# get_tectonic.ps1 — download the Tectonic LaTeX engine into bin\ (Windows).
# Tectonic is a single self-contained binary used by resume_brain/latex.py to compile the
# tailored résumé to PDF. We don't commit the ~50 MB binary (see .gitignore); run this once.
#   PowerShell:  ./scripts/get_tectonic.ps1
$ErrorActionPreference = 'Stop'
$ver = $env:TECTONIC_VERSION; if (-not $ver) { $ver = '0.16.9' }
$root = Split-Path -Parent $PSScriptRoot
$bin  = Join-Path $root 'bin'
New-Item -ItemType Directory -Force -Path $bin | Out-Null

$asset = "tectonic-$ver-x86_64-pc-windows-msvc.zip"
$url   = "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40$ver/$asset"
$tmp   = Join-Path $env:TEMP $asset
Write-Host "Downloading $asset ..."
Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
Expand-Archive -Path $tmp -DestinationPath $bin -Force
Remove-Item $tmp -Force
& (Join-Path $bin 'tectonic.exe') --version
Write-Host "Tectonic installed to $bin\tectonic.exe"
