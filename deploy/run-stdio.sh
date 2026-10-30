#!/usr/bin/env bash
# Intended for a Secure MCP Tunnel's --mcp-command.
# stdout MUST contain only MCP protocol responses; diagnostics go to stderr.
set -Eeuo pipefail
ROOT="${MCP_BRIDGE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_PREFIX="${MCP_BRIDGE_ENV_PREFIX:-${CODEX_MCP_ENV_PREFIX:-${CONDA_PREFIX:-$ROOT/.venv}}}"
cd "$ROOT"
export CODEX_MCP_WORKSPACE="${CODEX_MCP_WORKSPACE:-$ROOT/..}"
export MCP_BRIDGE_FULL_ACCESS="${MCP_BRIDGE_FULL_ACCESS:-true}"
export SSH_MCP_TRANSPORT=stdio
[[ -x "$ENV_PREFIX/bin/chatgpt-mcp-bridge" ]] || { echo "Missing Conda MCP executable: $ENV_PREFIX/bin/chatgpt-mcp-bridge" >&2; exit 1; }
export PATH="$ENV_PREFIX/bin:$PATH"
exec "$ENV_PREFIX/bin/chatgpt-mcp-bridge"
