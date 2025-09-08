#!/bin/bash
set -e

BASE_IMAGE="ghcr.io/jauderho/yt-dlp:latest"
DIGEST_FILE="base-image-digest.txt"

# Get current digest from registry
echo "🔍 Checking current digest for $BASE_IMAGE..."
CURRENT_DIGEST=$(docker manifest inspect "$BASE_IMAGE" | jq -r '.digest // .config.digest // .manifests[0].digest')

echo "Current digest: $CURRENT_DIGEST"

# Check if digest file exists
if [ ! -f "$DIGEST_FILE" ]; then
    echo "📝 No previous digest found, creating $DIGEST_FILE"
    echo "$CURRENT_DIGEST" > "$DIGEST_FILE"
    echo "build_needed=true" >> $GITHUB_OUTPUT
    exit 0
fi

# Read previous digest
PREVIOUS_DIGEST=$(cat "$DIGEST_FILE")
echo "Previous digest: $PREVIOUS_DIGEST"

# Compare digests
if [ "$CURRENT_DIGEST" != "$PREVIOUS_DIGEST" ]; then
    echo "🚨 Base image has changed!"
    echo "  Previous: $PREVIOUS_DIGEST"
    echo "  Current:  $CURRENT_DIGEST"
    echo "$CURRENT_DIGEST" > "$DIGEST_FILE"
    echo "build_needed=true" >> $GITHUB_OUTPUT
else
    echo "✅ Base image unchanged, skipping build"
    echo "build_needed=false" >> $GITHUB_OUTPUT
fi