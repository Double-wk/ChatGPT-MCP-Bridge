#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${CLOUDFLARED_BIN:-}"
if [[ -z "$BIN" ]]; then
  if [[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 && -x "$ROOT/cloudflared" ]]; then
    BIN="$ROOT/cloudflared"
  else
    BIN="$(command -v cloudflared || true)"
  fi
fi
[[ -n "$BIN" && -x "$BIN" ]] || {
  echo "No cloudflared binary for this platform; install cloudflared or set CLOUDFLARED_BIN" >&2
  exit 1
}
exec "$BIN" tunnel --no-autoupdate --http-host-header 127.0.0.1:8200 --url http://127.0.0.1:8200
