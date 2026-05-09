#!/bin/bash
# Install plugin.video.themoviedb.helper via Kodi JSON-RPC Addons.Install.
# Requires: jurialmunkey repo already extracted into /root/.kodi/addons/, Kodi running,
#           services.webserver.allowinstall = true.
# Usage: install_tmdbhelper.sh <kodi_jsonrpc_url> <user:password>
set -euo pipefail

KODI_URL="${1:?usage: install_tmdbhelper.sh <jsonrpc-url> <user:password>}"
KODI_AUTH="${2:?usage: install_tmdbhelper.sh <jsonrpc-url> <user:password>}"
ADDON_ID="plugin.video.themoviedb.helper"

echo "[tmdbhelper] Enabling jurialmunkey repository"
ENABLE_RESP="$(curl -fsS -u "$KODI_AUTH" -X POST "$KODI_URL/jsonrpc" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"Addons.SetAddonEnabled","params":{"addonid":"repository.jurialmunkey","enabled":true},"id":1}')"
echo "$ENABLE_RESP"
if ! echo "$ENABLE_RESP" | grep -q '"result":"OK"'; then
    echo "[tmdbhelper] FATAL: enable repository.jurialmunkey failed"
    exit 1
fi

echo "[tmdbhelper] Installing $ADDON_ID via Addons.Install (will pull deps)"
RESP="$(curl -fsS -u "$KODI_AUTH" -X POST "$KODI_URL/jsonrpc" \
  -H "Content-Type: application/json" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"Addons.Install\",\"params\":{\"addonid\":\"$ADDON_ID\"},\"id\":2}")"
echo "$RESP"
if echo "$RESP" | grep -q '"error"'; then
    echo "[tmdbhelper] FATAL: Addons.Install failed"
    exit 1
fi

echo "[tmdbhelper] Polling until addon is enabled"
for i in $(seq 1 60); do
    state="$(curl -fsS -u "$KODI_AUTH" -X POST "$KODI_URL/jsonrpc" \
      -H "Content-Type: application/json" \
      -d "{\"jsonrpc\":\"2.0\",\"method\":\"Addons.GetAddonDetails\",\"params\":{\"addonid\":\"$ADDON_ID\",\"properties\":[\"enabled\",\"installed\"]},\"id\":3}")"
    if echo "$state" | grep -q '"enabled":true'; then
        echo "[tmdbhelper] OK after ${i}s"
        exit 0
    fi
    sleep 1
done

echo "[tmdbhelper] FATAL: did not become enabled within 60s"
exit 1
