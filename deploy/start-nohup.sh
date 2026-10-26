#!/usr/bin/env bash
# Starts localhost HTTP MCP only when port is free; never overwrites the
# working service PID on a failed duplicate start.
set -Eeuo pipefail
ROOT="${MCP_BRIDGE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PORT="${SSH_MCP_HTTP_PORT:-8000}"
cd "$ROOT"
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "invalid TCP port" >&2; exit 1; }
command -v ss >/dev/null || { echo "Need ss (iproute2) to inspect port" >&2; exit 1; }
if ss -H -lnt | awk -v port=":$PORT" '$4 ~ (port "$") {seen=1} END {exit !seen}'; then
  echo "Port $PORT is already listening; no second MCP instance started."
  ss -lntp | grep ":$PORT" || true
  exit 0
fi
umask 077
mkdir -p logs
chmod 700 logs
nohup bash "$ROOT/deploy/run-http.sh" >> logs/mcp.log 2>&1 < /dev/null &
pid=$!
sleep 3
if kill -0 "$pid" 2>/dev/null && ss -H -lnt | awk -v port=":$PORT" '$4 ~ (port "$") {seen=1} END {exit !seen}'; then
  printf '%s\n' "$pid" > logs/mcp.pid
  echo "MCP started (PID $pid). Run deploy/doctor-mcp.py --probe-execute to verify."
else
  echo "MCP did not start successfully; see logs/mcp.log. PID file unchanged." >&2
  exit 1
fi
