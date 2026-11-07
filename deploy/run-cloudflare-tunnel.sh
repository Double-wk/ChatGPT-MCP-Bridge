#!/usr/bin/env bash
# Optional temporary HTTPS tunnel. Install cloudflared for the host platform.
set -Eeuo pipefail
BIN="${CLOUDFLARED_BIN:-$(command -v cloudflared || true)}"
[[ -n "$BIN" && -x "$BIN" ]] || {
  echo "Install cloudflared for this platform or set CLOUDFLARED_BIN" >&2
  exit 1
}
exec "$BIN" tunnel --no-autoupdate --http-host-header 127.0.0.1:8200 --url http://127.0.0.1:8200
