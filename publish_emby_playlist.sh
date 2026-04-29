#!/usr/bin/env bash
set -euo pipefail

SRC_FILE="${SRC_FILE:-playlist_emby_raw.m3u}"
PUBLISH_DIR="${PUBLISH_DIR:-published}"
DEST_FILE_NAME="${DEST_FILE_NAME:-playlist_emby_clean.m3u}"
DEST_FILE="${PUBLISH_DIR}/${DEST_FILE_NAME}"
TMP_FILE="${DEST_FILE}.tmp"

if [[ ! -f "$SRC_FILE" ]]; then
  echo "Source playlist not found: $SRC_FILE" >&2
  exit 1
fi

mkdir -p "$PUBLISH_DIR"

# Normalize line endings and remove empty lines as a minimal cleanup step.
tr -d '\r' < "$SRC_FILE" | awk 'NF' > "$TMP_FILE"

# Atomic replacement so readers always see a complete file.
mv -f "$TMP_FILE" "$DEST_FILE"

echo "Published $DEST_FILE via atomic rename (temp: $TMP_FILE)."
