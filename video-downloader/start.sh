#!/bin/bash
set -e

# Register Cloudflare WARP if not already registered
if [ ! -f /app/wgcf-account.toml ]; then
    echo "Registering Cloudflare WARP..."
    cd /app
    wgcf register --accept-tos
    wgcf generate
fi

# Bring up WireGuard tunnel
echo "Bringing up WARP tunnel..."
wg-quick up /app/wgcf-profile.conf || true

# Verify WARP IP
echo "Public IP via WARP:"
curl -s --max-time 10 https://cloudflare.com/cdn-cgi/trace | grep -E "^(ip|warp)" || echo "Could not verify WARP IP"

# Start FastAPI app
cd /app
echo "Starting Uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
