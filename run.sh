#!/usr/bin/env bash
# Start NeuroCut MCP server + a Cloudflare quick tunnel; print the connector URL.
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
PORT="${NEUROCUT_PORT:-8766}"
mkdir -p .run
[ -f .run/token ] || openssl rand -hex 24 > .run/token
TOKEN="$(cat .run/token)"

for pid in $(ss -tlnpH "sport = :${PORT}" 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u); do kill "$pid" 2>/dev/null || true; done
pkill -f "cloudflared tunnel --no-autoupdate --url http://127.0.0.1:${PORT}" 2>/dev/null || true
sleep 1

NEUROCUT_TOKEN="$TOKEN" NEUROCUT_PORT="$PORT" \
  NEUROCUT_EXPORT="${NEUROCUT_EXPORT:-$HOME/neurocut-output}" \
  nohup "$PY" -m neurocut > .run/server.log 2>&1 &
echo "server pid $!"

command -v cloudflared >/dev/null || { echo "install cloudflared for a public URL"; exit 0; }
nohup cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:${PORT}" > .run/tunnel.log 2>&1 &
echo "cloudflared pid $!"
for _ in $(seq 1 30); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' .run/tunnel.log | head -1 || true)"
  [ -n "$URL" ] && break; sleep 1
done
echo "$URL" > .run/url
cat <<MSG

  Connector URL : ${URL:-<check .run/tunnel.log>}/mcp
  Header        : X-Neurocut-Token: $TOKEN
  Auth in Claude: None

MSG
