#!/bin/bash
# Install the jurialmunkey Kodi repository into the kodi container BEFORE Kodi starts.
# Usage: install_jurialmunkey_repo.sh <container_name>
set -euo pipefail

CONTAINER="${1:?usage: install_jurialmunkey_repo.sh <container>}"
BASE_URL="https://jurialmunkey.github.io/repository.jurialmunkey/"
CURL_TIMEOUT_OPTS=(--connect-timeout 10 --max-time 60)
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "[jurialmunkey] Discovering latest repo zip from $BASE_URL"
INDEX_HTML="$(curl -fsSL "${CURL_TIMEOUT_OPTS[@]}" "$BASE_URL")"
ZIP_NAME="$(echo "$INDEX_HTML" \
  | grep -oE 'repository\.jurialmunkey-[0-9.]+\.zip' \
  | sort -V | tail -n1)"

if [[ -z "$ZIP_NAME" ]]; then
    echo "[jurialmunkey] FATAL: could not find a repository.jurialmunkey-*.zip on $BASE_URL"
    exit 1
fi

ZIP_URL="${BASE_URL}${ZIP_NAME}"
echo "[jurialmunkey] Downloading $ZIP_URL"
curl -fsSL "${CURL_TIMEOUT_OPTS[@]}" -o "$WORKDIR/$ZIP_NAME" "$ZIP_URL"

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

# Kodi only scans /root/.kodi/addons/ at startup. Without a restart, the
# subsequent install_tmdbhelper.sh -> Addons.SetAddonEnabled call returns
# -32602 Invalid params because the addonid is not yet in Kodi's DB.
echo "[jurialmunkey] Restarting Kodi container so it discovers the new repo"
docker restart "$CONTAINER" >/dev/null
echo "[jurialmunkey] Waiting for Kodi JSON-RPC to come back up"
deadline=$(( $(date +%s) + 90 ))
while (( $(date +%s) < deadline )); do
    if curl -fsS -u kodi:kodi -m 2 -X POST "http://localhost:8082/jsonrpc" \
         -H "Content-Type: application/json" \
         -d '{"jsonrpc":"2.0","method":"Application.GetProperties","params":{"properties":["version"]},"id":1}' \
         2>/dev/null | grep -q '"major":21'; then
        echo "[jurialmunkey] OK (Kodi back up; repo.jurialmunkey scanned)"
        exit 0
    fi
    sleep 2
done
echo "[jurialmunkey] FATAL: Kodi did not return on JSON-RPC within 90s after restart"
exit 1
