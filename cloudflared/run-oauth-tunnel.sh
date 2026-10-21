#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${CLOUDFLARED_BIN:-$ROOT/cloudflared}"
if [[ ! -x "$BIN" ]]; then
  BIN="$(command -v cloudflared || true)"
fi
[[ -n "$BIN" && -x "$BIN" ]] || {
  echo "cloudflared binary is missing; install it or set CLOUDFLARED_BIN" >&2
  exit 1
}
exec "$BIN" tunnel --no-autoupdate --http-host-header 127.0.0.1:8200 --url http://127.0.0.1:8200
