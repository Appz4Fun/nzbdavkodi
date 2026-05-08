#!/bin/bash
set -e

echo "Starting Kodi + VNC test environment..."
docker-compose up -d

echo "Waiting for Kodi to be ready (this may take 1-2 minutes)..."
sleep 30

echo "Installing test dependencies..."
pip install -q requests pytest

echo "Running functional tests..."
python -m pytest tests/test_functional_playback.py -v

echo ""
echo "=========================================="
echo "Test environment is ready!"
echo "=========================================="
echo ""
echo "Kodi JSON-RPC API: http://localhost:8080/jsonrpc"
echo "  Username: kodi"
echo "  Password: kodi"
echo "VNC Web Interface: http://localhost:6901"
echo "VNC Direct (port): localhost:5901 (password: test123)"
echo ""
echo "To stop the environment:"
echo "  docker-compose down"
echo ""
