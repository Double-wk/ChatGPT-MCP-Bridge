#!/usr/bin/env bash
# Install this checkout; do not publish or restart services.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${MCP_BRIDGE_ENV_PREFIX:-${CODEX_MCP_ENV_PREFIX:-${CONDA_PREFIX:-}}}"
[[ -n "$PREFIX" && -x "$PREFIX/bin/python" ]] || { echo 'Activate Conda first: conda activate chatgpt-mcp-bridge' >&2; exit 1; }
"$PREFIX/bin/python" -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ is required"'
bash "$ROOT/deploy/build-runtime.sh"
"$PREFIX/bin/python" -m pip install -e "$ROOT[gateway]"
CONFIG_DIR="$HOME/.config/ssh-mcp"
install -d -m 700 "$CONFIG_DIR"
for name in http-token gateway-token; do
  if [[ ! -e "$CONFIG_DIR/$name" ]]; then
    (umask 077; "$PREFIX/bin/python" -c 'import secrets;print(secrets.token_urlsafe(48))' > "$CONFIG_DIR/$name")
  fi
done
echo "Installed: $PREFIX/bin/chatgpt-mcp-bridge"
echo 'Set CODEX_MCP_WORKSPACE, then run deploy/run-http.sh or deploy/run-stdio.sh.'
