#!/bin/bash
set -e

COMPOSE="docker compose -f docker-compose.full-test.yml"
NZBDAV_API_KEY="testkey-dev"
NZBDAV_HOST="http://localhost:8180"
WEBDAV_USER="admin"
WEBDAV_PASS="devpass"

# Load optional credentials from full-test.env
if [[ -f full-test.env ]]; then
    set -a; source full-test.env; set +a
fi

echo "Starting full test stack (nzbdav-rs + NZBHydra2 + Kodi + VNC)..."
$COMPOSE up -d

echo "Waiting for nzbdav-rs to be healthy..."
for i in $(seq 1 30); do
    if $COMPOSE ps nzbdav | grep -q "healthy"; then
        echo "  nzbdav-rs is healthy."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  nzbdav-rs did not become healthy in time."
        $COMPOSE logs nzbdav
        exit 1
    fi
    sleep 3
done

# ── Configure Usenet server ────────────────────────────────────────────────────
if [[ -n "${NNTP_HOST:-}" ]]; then
    existing=$(curl -s "$NZBDAV_HOST/api/servers" -H "X-Api-Key: $NZBDAV_API_KEY" | python3 -c "
import sys, json; servers=json.load(sys.stdin)
match = [s for s in servers if s.get('host') == '$NNTP_HOST']
print(len(match))
" 2>/dev/null || echo 0)
    if [[ "$existing" -eq 0 ]]; then
        echo "Adding Usenet server: ${NNTP_HOST}..."
        curl -s -X POST "$NZBDAV_HOST/api/servers" \
            -H "X-Api-Key: $NZBDAV_API_KEY" \
            -H "Content-Type: application/json" \
            -d "{
                \"id\": \"\",
                \"name\": \"Usenet\",
                \"host\": \"${NNTP_HOST}\",
                \"port\": ${NNTP_PORT:-563},
                \"ssl\": true,
                \"ssl_verify\": true,
                \"username\": \"${NNTP_USER}\",
                \"password\": \"${NNTP_PASS}\",
                \"connections\": ${NNTP_CONNECTIONS:-4},
                \"enabled\": true
            }" > /dev/null
        echo "  Usenet server added."
    else
        echo "  Usenet server already configured."
    fi
fi

# ── Configure NZBHydra2 indexer ───────────────────────────────────────────────
echo "Waiting for NZBHydra2..."
for i in $(seq 1 20); do
    if curl -s -o /dev/null -w "%{http_code}" "http://localhost:5076/" | grep -q "200"; then
        break
    fi
    sleep 3
done

if [[ -n "${INDEXER_URL:-}" && -n "${INDEXER_API_KEY:-}" ]]; then
    existing_indexers=$(curl -s "http://localhost:5076/internalapi/config" | python3 -c "
import sys, json; d=json.load(sys.stdin)
match = [i for i in d.get('indexers', []) if i.get('host') == '$INDEXER_URL']
print(len(match))
" 2>/dev/null || echo 0)
    if [[ "$existing_indexers" -eq 0 ]]; then
        echo "Adding indexer: ${INDEXER_URL}..."
        full_config=$(curl -s "http://localhost:5076/internalapi/config")
        updated=$(echo "$full_config" | python3 -c "
import sys, json
config = json.load(sys.stdin)
config['indexers'] = [{
    'name': 'Indexer',
    'enabled': True,
    'host': '${INDEXER_URL}',
    'apiKey': '${INDEXER_API_KEY}',
    'searchModuleType': 'NEWZNAB',
    'configName': 'Newznab',
    'state': 'ENABLED',
    'showOnSearch': True,
    'preselect': True,
}]
print(json.dumps(config))
")
        curl -s -X PUT "http://localhost:5076/internalapi/config" \
            -H "Content-Type: application/json" \
            -d "$updated" > /dev/null
        echo "  Indexer added."
    else
        echo "  Indexer already configured."
    fi

    # Seed Kodi addon settings with the live NZBHydra2 API key
    hydra_key=$(curl -s "http://localhost:5076/internalapi/config" | python3 -c "
import sys, json; d=json.load(sys.stdin); print(d.get('main',{}).get('apiKey',''))
" 2>/dev/null || echo "")
    if [[ -n "$hydra_key" ]]; then
        $COMPOSE exec -T kodi sh -c "mkdir -p /config/.kodi/userdata/addon_data/plugin.video.nzbdav && cat > /config/.kodi/userdata/addon_data/plugin.video.nzbdav/settings.xml << 'XMLEOF'
<?xml version=\"1.0\" encoding=\"utf-8\" standalone=\"yes\"?>
<settings version=\"2\">
    <setting id=\"nzbhydra_enabled\">true</setting>
    <setting id=\"hydra_url\">http://hydra:5076</setting>
    <setting id=\"hydra_api_key\">${hydra_key}</setting>
    <setting id=\"prowlarr_enabled\">false</setting>
    <setting id=\"nzbdav_url\">http://nzbdav:8080</setting>
    <setting id=\"nzbdav_api_key\">testkey-dev</setting>
    <setting id=\"webdav_url\">http://nzbdav:8080/dav</setting>
    <setting id=\"webdav_username\">admin</setting>
    <setting id=\"webdav_password\">devpass</setting>
</settings>
XMLEOF" 2>/dev/null && echo "  Kodi addon settings seeded." || true
    fi
fi

echo "Waiting for Kodi to settle (30s)..."
sleep 30

echo "Installing test dependencies..."
.venv/bin/pip install -q requests pytest

echo "Running full-stack tests..."
.venv/bin/python -m pytest tests/test_full_stack.py -v

echo ""
echo "=========================================="
echo "Full test stack is running!"
echo "=========================================="
echo ""
echo "nzbdav-rs:         http://localhost:8180/ui"
echo "  API key:         testkey-dev"
echo "  WebDAV:          http://localhost:8180/dav  (admin / devpass)"
echo ""
echo "NZBHydra2:         http://localhost:5076"
echo ""
echo "Kodi JSON-RPC:     http://localhost:8080/jsonrpc  (kodi / kodi)"
echo "Kodi VNC:          localhost:5902"
echo ""
echo "VNC Web:           http://localhost:6901  (password: test123)"
echo "VNC Direct:        localhost:5901"
echo ""
echo "To run smoke test (requires Usenet server + a local NZB in dev/nzbs/):"
echo "  NZBDAV_URL=$NZBDAV_HOST NZBDAV_API_KEY=$NZBDAV_API_KEY \\"
echo "  WEBDAV_URL=$NZBDAV_HOST/dav WEBDAV_USER=$WEBDAV_USER WEBDAV_PASS=$WEBDAV_PASS \\"
echo "  .venv/bin/python dev/smoke.py dev/nzbs/<file.nzb>"
echo ""
echo "To stop the stack:"
echo "  docker compose -f docker-compose.full-test.yml down"
