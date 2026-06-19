#!/usr/bin/env bash
# get_tectonic.sh — download the Tectonic LaTeX engine into bin/ (Linux/macOS, e.g. the host).
# Tectonic is a single self-contained binary used by resume_brain/latex.py to compile the
# tailored résumé to PDF. We don't commit the ~50 MB binary (see .gitignore); run this once.
#   bash scripts/get_tectonic.sh
set -euo pipefail
VER="${TECTONIC_VERSION:-0.16.9}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/bin"
mkdir -p "$BIN"

case "$(uname -s)" in
  Linux)  TARGET="x86_64-unknown-linux-gnu" ;;
  Darwin) TARGET="x86_64-apple-darwin" ;;
  *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac
ASSET="tectonic-${VER}-${TARGET}.tar.gz"
URL="https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${VER}/${ASSET}"

echo "Downloading ${ASSET} ..."
TMP="$(mktemp -d)"
curl -fsSL "$URL" -o "$TMP/$ASSET"
tar -xzf "$TMP/$ASSET" -C "$BIN" tectonic
rm -rf "$TMP"
chmod +x "$BIN/tectonic"
"$BIN/tectonic" --version
echo "Tectonic installed to $BIN/tectonic"
