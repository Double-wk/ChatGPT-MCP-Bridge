#!/usr/bin/env bash
# Run the already installed Conda executable directly. Do not depend on shell activation.
set -Eeuo pipefail
ROOT="${MCP_BRIDGE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_PREFIX="${MCP_BRIDGE_ENV_PREFIX:-${CODEX_MCP_ENV_PREFIX:-${CONDA_PREFIX:-$ROOT/.venv}}}"
cd "$ROOT"
export CODEX_MCP_WORKSPACE="${CODEX_MCP_WORKSPACE:-$ROOT/..}"
export MCP_BRIDGE_FULL_ACCESS="${MCP_BRIDGE_FULL_ACCESS:-true}"
export SSH_MCP_TRANSPORT=http
export SSH_MCP_HTTP_HOST=127.0.0.1
export SSH_MCP_HTTP_PORT="${SSH_MCP_HTTP_PORT:-8000}"
export SSH_MCP_HTTP_AUTH=bearer
export SSH_MCP_HTTP_TOKEN_FILE="${SSH_MCP_HTTP_TOKEN_FILE:-$HOME/.config/ssh-mcp/http-token}"
[[ -f "$SSH_MCP_HTTP_TOKEN_FILE" ]] || { echo "Missing HTTP token file (never print its contents)" >&2; exit 1; }
[[ -x "$ENV_PREFIX/bin/chatgpt-mcp-bridge" ]] || { echo "Conda executable missing: $ENV_PREFIX/bin/chatgpt-mcp-bridge; set CODEX_MCP_ENV_PREFIX" >&2; exit 1; }
export PATH="$ENV_PREFIX/bin:$PATH"
exec "$ENV_PREFIX/bin/chatgpt-mcp-bridge"
