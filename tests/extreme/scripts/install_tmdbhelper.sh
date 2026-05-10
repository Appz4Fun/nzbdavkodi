#!/bin/bash
# Install plugin.video.themoviedb.helper by downloading the addon zip plus
# its jurialmunkey-side dependencies (script.module.tmdbhelper.api.*) from
# the jurialmunkey repository's omega/zips/ tree, dropping each into the
# Kodi container's addons/, then restarting Kodi so it scans them. Used
# instead of Kodi's JSON-RPC Addons.Install (does not exist in v21) and
# the InstallAddon builtin (calls InstallModal which requires a GUI
# confirmation that no one is around to click in our headless Xvfb).
# Standard official-repo deps (script.module.requests, etc.) are
# auto-resolved on Kodi's next start via the already-enabled
# repository.xbmc.org.
# Usage: install_tmdbhelper.sh <kodi_jsonrpc_url> <user:password>
set -euo pipefail

KODI_URL="${1:?usage: install_tmdbhelper.sh <jsonrpc-url> <user:password>}"
KODI_AUTH="${2:?usage: install_tmdbhelper.sh <jsonrpc-url> <user:password>}"
ADDON_ID="plugin.video.themoviedb.helper"
CONTAINER="nzbdav-extreme-kodi"
REPO_BASE="https://raw.githubusercontent.com/jurialmunkey/repository.jurialmunkey/master/omega/zips"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

rpc() {
    curl -fsS -u "$KODI_AUTH" -X POST "$KODI_URL/jsonrpc" \
      -H "Content-Type: application/json" -d "$1"
}

echo "[tmdbhelper] Fetching jurialmunkey addons.xml"
curl -fsSL -o "$WORKDIR/addons.xml" "$REPO_BASE/addons.xml"

echo "[tmdbhelper] Resolving dependency tree from jurialmunkey's addons.xml"
# Walk tmdbhelper's <requires> graph. For each requirement found in
# jurialmunkey's manifest, include it (and recurse). Requirements not in
# jurialmunkey's repo are assumed to come from repository.xbmc.org (the
# official Kodi addons repo, which Kodi auto-installs deps from on
# restart) or to be built into the Kodi runtime (e.g. xbmc.python).
mapfile -t TARGETS < <(python3 - "$WORKDIR/addons.xml" "$ADDON_ID" <<'PY'
import sys
import xml.etree.ElementTree as ET

xml_path, root_id = sys.argv[1], sys.argv[2]
root = ET.parse(xml_path).getroot()

by_id = {}
for addon in root.findall("addon"):
    aid = addon.get("id", "")
    ver = addon.get("version", "")
    requires = []
    req_node = addon.find("requires")
    if req_node is not None:
        for imp in req_node.findall("import"):
            requires.append(imp.get("addon", ""))
    by_id[aid] = (ver, requires)

if root_id not in by_id:
    sys.stderr.write(f"FATAL: {root_id} not in jurialmunkey addons.xml\n")
    sys.exit(1)

selected = {}  # aid -> ver
stack = [root_id]
while stack:
    aid = stack.pop()
    if aid in selected:
        continue
    if aid not in by_id:
        # Comes from another repo (xbmc.org) or is a built-in (xbmc.python).
        # Kodi resolves these on restart from repository.xbmc.org.
        continue
    ver, deps = by_id[aid]
    selected[aid] = ver
    stack.extend(deps)

for aid in sorted(selected):
    print(f"{aid} {selected[aid]}")
PY
)

if [[ ${#TARGETS[@]} -eq 0 ]]; then
    echo "[tmdbhelper] FATAL: no targets found in $REPO_BASE/addons.xml"
    exit 1
fi
echo "[tmdbhelper] Targets:"
printf '  - %s\n' "${TARGETS[@]}"

echo "[tmdbhelper] Downloading and extracting each target into the container"
docker exec "$CONTAINER" mkdir -p /root/.kodi/addons
for line in "${TARGETS[@]}"; do
    aid="${line% *}"
    ver="${line#* }"
    zip_url="$REPO_BASE/$aid/$aid-$ver.zip"
    zip_path="$WORKDIR/$aid-$ver.zip"
    echo "[tmdbhelper]   $aid v$ver  <-  $zip_url"
    curl -fsSL -o "$zip_path" "$zip_url"
    # Extract on host (cleaner failure than relying on unzip-in-container),
    # then docker cp the resulting addon dir.
    extract_dir="$WORKDIR/extracted_$aid"
    mkdir -p "$extract_dir"
    unzip -q -o "$zip_path" -d "$extract_dir"
    if [[ ! -d "$extract_dir/$aid" ]]; then
        echo "[tmdbhelper] FATAL: $zip_path did not contain $aid/"
        exit 1
    fi
    docker exec "$CONTAINER" rm -rf "/root/.kodi/addons/$aid"
    docker cp "$extract_dir/$aid" "$CONTAINER:/root/.kodi/addons/$aid"
done

echo "[tmdbhelper] Restarting Kodi container so it discovers new addons"
docker restart "$CONTAINER" >/dev/null
echo "[tmdbhelper] Waiting for Kodi JSON-RPC to come back up"
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
    echo "[tmdbhelper] FATAL: Kodi did not return on JSON-RPC within 90s after restart"
    exit 1
fi

echo "[tmdbhelper] Enabling each target"
fail=0
for line in "${TARGETS[@]}"; do
    aid="${line% *}"
    resp="$(rpc "{\"jsonrpc\":\"2.0\",\"method\":\"Addons.SetAddonEnabled\",\"params\":{\"addonid\":\"$aid\",\"enabled\":true},\"id\":1}")"
    if echo "$resp" | grep -q '"result":"OK"'; then
        echo "[tmdbhelper]   $aid -> enabled"
    else
        echo "[tmdbhelper]   $aid -> NOT enabled: $resp"
        fail=1
    fi
done

if [[ $fail -ne 0 ]]; then
    echo "[tmdbhelper] FATAL: one or more targets did not enable"
    echo "[tmdbhelper] --- diagnostics ---"
    echo "[tmdbhelper] /root/.kodi/addons inside container:"
    docker exec "$CONTAINER" ls -la /root/.kodi/addons || true
    echo "[tmdbhelper] tail of kodi.log (broken/install/dependency):"
    docker exec "$CONTAINER" sh -c \
      'log=$(ls /root/.kodi/temp/kodi*.log 2>/dev/null | head -1); \
       if [ -n "$log" ]; then \
         grep -iE "broken|missing|depend|install|themoviedb|tmdbhelper" "$log" | tail -80; \
       else echo "(no kodi.log)"; fi' || true
    exit 1
fi

echo "[tmdbhelper] Verifying $ADDON_ID is installed and enabled"
for i in $(seq 1 60); do
    state="$(rpc "{\"jsonrpc\":\"2.0\",\"method\":\"Addons.GetAddonDetails\",\"params\":{\"addonid\":\"$ADDON_ID\",\"properties\":[\"enabled\",\"installed\",\"broken\"]},\"id\":2}")"
    if echo "$state" | grep -q '"enabled":true' && echo "$state" | grep -q '"installed":true'; then
        echo "[tmdbhelper] OK after ${i}s"
        echo "$state"
        exit 0
    fi
    sleep 1
done

echo "[tmdbhelper] FATAL: $ADDON_ID not in installed+enabled state within 60s"
echo "$state"
echo "[tmdbhelper] --- diagnostics ---"
docker exec "$CONTAINER" sh -c \
  'log=$(ls /root/.kodi/temp/kodi*.log 2>/dev/null | head -1); \
   [ -n "$log" ] && grep -iE "broken|missing|depend|themoviedb|tmdbhelper" "$log" | tail -80'
exit 1
