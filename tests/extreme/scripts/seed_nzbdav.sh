#!/bin/bash
# Seed nzbdav-rs with the configured NNTP provider via its REST API.
# Idempotent: skips if a server with this host already exists.
set -eu

NZBDAV_URL="${NZBDAV_URL:-http://localhost:8180}"
API_KEY="${NZBDAV_API_KEY:?missing NZBDAV_API_KEY}"
NNTP_HOST="${NNTP_HOST:?missing NNTP_HOST}"
NNTP_PORT="${NNTP_PORT:-563}"
NNTP_USER="${NNTP_USER:?missing NNTP_USER}"
NNTP_PASS="${NNTP_PASS:?missing NNTP_PASS}"
NNTP_CONNS="${NNTP_CONNS:-50}"

existing="$(curl -fs -H "X-Api-Key: $API_KEY" "$NZBDAV_URL/api/servers" || echo '[]')"

if echo "$existing" | grep -q "\"host\":\"$NNTP_HOST\""; then
    echo "[seed] $NNTP_HOST already configured; skipping"
    exit 0
fi

echo "[seed] Adding $NNTP_HOST:$NNTP_PORT to nzbdav-rs..."
curl -fs -H "X-Api-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d "{\"id\":\"\",\"name\":\"$NNTP_HOST\",\"host\":\"$NNTP_HOST\",\"port\":$NNTP_PORT,\"ssl\":true,\"ssl_verify\":true,\"username\":\"$NNTP_USER\",\"password\":\"$NNTP_PASS\",\"connections\":$NNTP_CONNS,\"priority\":0,\"enabled\":true,\"retention\":0}" \
  "$NZBDAV_URL/api/servers"
echo
echo "[seed] OK"
