#!/bin/bash
# Seed nzbdav/nzbdav with the configured API key, WebDAV credentials, and
# NNTP provider via its upstream config API.
set -eu

NZBDAV_URL="${NZBDAV_URL:-http://localhost:8180}"
API_KEY="${NZBDAV_API_KEY:?missing NZBDAV_API_KEY}"
NNTP_HOST="${NNTP_HOST:?missing NNTP_HOST}"
NNTP_USE_SSL="${NNTP_USE_SSL:-false}"
if [ -z "${NNTP_PORT:-}" ]; then
    if [ "$NNTP_USE_SSL" = "true" ]; then
        NNTP_PORT="563"
    else
        NNTP_PORT="119"
    fi
fi
NNTP_USER="${NNTP_USER:?missing NNTP_USER}"
NNTP_PASS="${NNTP_PASS:?missing NNTP_PASS}"
NNTP_CONNS="${NNTP_CONNS:-50}"
WEBDAV_USERNAME="${WEBDAV_USERNAME:-admin}"
WEBDAV_PASSWORD="${WEBDAV_PASSWORD:?missing WEBDAV_PASSWORD}"

provider_config="$(
python3 -c 'import json, os
config = {
    "Providers": [
        {
            "Type": 1,
            "Host": os.environ["NNTP_HOST"],
            "Port": int(os.environ["NNTP_PORT"]),
            "UseSsl": os.environ.get("NNTP_USE_SSL", "false").lower() == "true",
            "User": os.environ["NNTP_USER"],
            "Pass": os.environ["NNTP_PASS"],
            "MaxConnections": int(os.environ.get("NNTP_CONNS", "50")),
        }
    ]
}
print(json.dumps(config, separators=(",", ":")))'
)"

echo "[seed] Configuring nzbdav upstream image for $NNTP_HOST:$NNTP_PORT..."
curl -fs -o /dev/null -X POST -H "X-Api-Key: $API_KEY" \
  -F "api.key=$API_KEY" \
  -F "webdav.user=$WEBDAV_USERNAME" \
  -F "webdav.pass=$WEBDAV_PASSWORD" \
  -F "usenet.providers=$provider_config" \
  "$NZBDAV_URL/api/update-config"
echo "[seed] OK"
