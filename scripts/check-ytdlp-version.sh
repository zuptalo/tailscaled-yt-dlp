#!/usr/bin/env bash
# Compare latest yt-dlp on PyPI with yt-dlp-version.txt (same logic as CI).
# If GITHUB_OUTPUT is set, appends build_needed=true|false for GitHub Actions.
#
# Usage:
#   ./scripts/check-ytdlp-version.sh           # update yt-dlp-version.txt when drift detected
#   ./scripts/check-ytdlp-version.sh --dry-run # print only; do not write yt-dlp-version.txt
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

command -v curl >/dev/null 2>&1 || { echo "error: curl required" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "error: jq required" >&2; exit 2; }

CURRENT_YTDLP="$(curl -sS https://pypi.org/pypi/yt-dlp/json | jq -r '.info.version')"
echo "Latest yt-dlp version: $CURRENT_YTDLP"

VERSION_FILE="yt-dlp-version.txt"
BUILD_NEEDED=false

if [[ ! -f "$VERSION_FILE" ]]; then
  echo "No previous version file found"
  if [[ "$DRY_RUN" == true ]]; then
    echo "Would write $CURRENT_YTDLP to $VERSION_FILE"
  else
    echo "$CURRENT_YTDLP" > "$VERSION_FILE"
  fi
  BUILD_NEEDED=true
else
  PREVIOUS_YTDLP="$(cat "$VERSION_FILE")"
  echo "Previous yt-dlp version: $PREVIOUS_YTDLP"
  if [[ "$CURRENT_YTDLP" != "$PREVIOUS_YTDLP" ]]; then
    echo "yt-dlp version changed!"
    if [[ "$DRY_RUN" == true ]]; then
      echo "Would update $VERSION_FILE to $CURRENT_YTDLP"
    else
      echo "$CURRENT_YTDLP" > "$VERSION_FILE"
    fi
    BUILD_NEEDED=true
  else
    echo "yt-dlp version unchanged."
    BUILD_NEEDED=false
  fi
fi

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  if [[ "$BUILD_NEEDED" == true ]]; then
    echo "build_needed=true" >> "$GITHUB_OUTPUT"
  else
    echo "build_needed=false" >> "$GITHUB_OUTPUT"
  fi
fi
