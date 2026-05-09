#!/bin/bash
# Install the jurialmunkey Kodi repository into the kodi container BEFORE Kodi starts.
# Usage: install_jurialmunkey_repo.sh <container_name>
set -euo pipefail

CONTAINER="${1:?usage: install_jurialmunkey_repo.sh <container>}"
BASE_URL="https://jurialmunkey.github.io/repository.jurialmunkey/"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "[jurialmunkey] Discovering latest repo zip from $BASE_URL"
INDEX_HTML="$(curl -fsSL "$BASE_URL")"
ZIP_NAME="$(echo "$INDEX_HTML" \
  | grep -oE 'repository\.jurialmunkey-[0-9.]+\.zip' \
  | sort -V | tail -n1)"

if [[ -z "$ZIP_NAME" ]]; then
    echo "[jurialmunkey] FATAL: could not find a repository.jurialmunkey-*.zip on $BASE_URL"
    exit 1
fi

ZIP_URL="${BASE_URL}${ZIP_NAME}"
echo "[jurialmunkey] Downloading $ZIP_URL"
curl -fsSL -o "$WORKDIR/$ZIP_NAME" "$ZIP_URL"

echo "[jurialmunkey] Extracting on host"
unzip -q "$WORKDIR/$ZIP_NAME" -d "$WORKDIR/extracted"

if [[ ! -d "$WORKDIR/extracted/repository.jurialmunkey" ]]; then
    echo "[jurialmunkey] FATAL: zip did not contain expected repository.jurialmunkey/ directory"
    exit 1
fi

echo "[jurialmunkey] Copying into container $CONTAINER"
docker exec "$CONTAINER" mkdir -p /root/.kodi/addons /root/.kodi/userdata
docker cp "$WORKDIR/extracted/repository.jurialmunkey" \
    "$CONTAINER:/root/.kodi/addons/repository.jurialmunkey"

echo "[jurialmunkey] OK (extracted into /root/.kodi/addons/repository.jurialmunkey)"
