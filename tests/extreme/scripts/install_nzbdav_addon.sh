#!/bin/bash
# Build the nzbdav addon zip via `just repo-zip`, copy into the kodi container,
# extract into addons/, render settings.xml from the template, and enable.
# Usage: install_nzbdav_addon.sh <container> <kodi_jsonrpc_url> <user:password> <template_path>
set -euo pipefail

CONTAINER="${1:?usage: install_nzbdav_addon.sh <container> <jsonrpc-url> <user:password> <template>}"
KODI_URL="${2:?usage}"
KODI_AUTH="${3:?usage}"
TEMPLATE_PATH="${4:?usage}"

REPO_ROOT="$(git rev-parse --show-toplevel)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "[nzbdav-addon] just repo-zip"
( cd "$REPO_ROOT" && just repo-zip )

# `just repo-zip` writes to dist/; find the most recent zip
ZIP="$(ls -t "$REPO_ROOT"/dist/plugin.video.nzbdav-*.zip 2>/dev/null | head -n1)"
if [[ -z "$ZIP" ]]; then
    echo "[nzbdav-addon] FATAL: no zip found in $REPO_ROOT/dist/"
    exit 1
fi
echo "[nzbdav-addon] Using $ZIP"

echo "[nzbdav-addon] Extracting on host"
# The kodi-desktop image does not ship `unzip`, so we extract on the host
# (same pattern install_jurialmunkey_repo.sh and install_tmdbhelper.sh use)
# and docker cp the resulting directory in.
EXTRACT_DIR="$WORKDIR/extracted"
mkdir -p "$EXTRACT_DIR"
unzip -q -o "$ZIP" -d "$EXTRACT_DIR"
if [[ ! -d "$EXTRACT_DIR/plugin.video.nzbdav" ]]; then
    echo "[nzbdav-addon] FATAL: $ZIP did not contain plugin.video.nzbdav/"
    exit 1
fi

echo "[nzbdav-addon] Copying into container"
docker exec "$CONTAINER" mkdir -p /root/.kodi/addons
docker exec "$CONTAINER" rm -rf /root/.kodi/addons/plugin.video.nzbdav
docker cp "$EXTRACT_DIR/plugin.video.nzbdav" \
    "$CONTAINER:/root/.kodi/addons/plugin.video.nzbdav"

echo "[nzbdav-addon] Rendering settings.xml from template"
RENDERED="$WORKDIR/settings.xml"
# Constrain envsubst to a known set of variables, avoiding accidental
# substitution (and silent empty-replacement) of any other $VAR token
# that might appear inside the template's XML. The addon's HTTP client
# bypasses OrbStack's transparent proxy for RFC1918 destinations via
# NO_PROXY (set on the kodi-desktop service in docker-compose.yml), so
# LAN-hosted Hydra URLs work without rewriting.
SUBST_VARS='${HYDRA_URL} ${HYDRA_API_KEY} ${NZBDAV_API_KEY} ${WEBDAV_USERNAME} ${WEBDAV_PASSWORD}'
for var_name in HYDRA_URL HYDRA_API_KEY NZBDAV_API_KEY WEBDAV_USERNAME WEBDAV_PASSWORD; do
    if [[ -z "${!var_name:-}" ]]; then
        echo "[nzbdav-addon] FATAL: required env var $var_name is empty or unset"
        exit 1
    fi
done
envsubst "$SUBST_VARS" < "$TEMPLATE_PATH" > "$RENDERED"
docker exec "$CONTAINER" mkdir -p /root/.kodi/userdata/addon_data/plugin.video.nzbdav
docker cp "$RENDERED" "$CONTAINER:/root/.kodi/userdata/addon_data/plugin.video.nzbdav/settings.xml"

# Same reason as install_jurialmunkey_repo.sh: Kodi only scans the addons
# folder at startup, so SetAddonEnabled below would return -32602 without
# a restart to register plugin.video.nzbdav in Kodi's DB.
echo "[nzbdav-addon] Restarting Kodi container so it discovers the addon"
docker restart "$CONTAINER" >/dev/null
echo "[nzbdav-addon] Waiting for Kodi JSON-RPC to come back up"
deadline=$(( $(date +%s) + 90 ))
while (( $(date +%s) < deadline )); do
    if curl -fsS -u "$KODI_AUTH" -m 2 -X POST "$KODI_URL/jsonrpc" \
         -H "Content-Type: application/json" \
         -d '{"jsonrpc":"2.0","method":"Application.GetProperties","params":{"properties":["version"]},"id":1}' \
         2>/dev/null | grep -q '"major":21'; then
        break
    fi
    sleep 2
done
if (( $(date +%s) >= deadline )); then
    echo "[nzbdav-addon] FATAL: Kodi did not return on JSON-RPC within 90s after restart"
    exit 1
fi

echo "[nzbdav-addon] Enabling addon via JSON-RPC"
curl -fsS -u "$KODI_AUTH" -X POST "$KODI_URL/jsonrpc" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"Addons.SetAddonEnabled","params":{"addonid":"plugin.video.nzbdav","enabled":true},"id":1}' \
  | tee /dev/stderr | grep -q '"result":"OK"'

echo "[nzbdav-addon] OK"
