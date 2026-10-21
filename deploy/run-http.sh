#!/usr/bin/env bash
# Run the already installed Conda executable directly. Do not depend on shell activation.
set -Eeuo pipefail
ROOT="${SSH_MCP_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_PREFIX="${SSH_MCP_CONDA_PREFIX:-/mnt/tcm_agent_data/Double/envs/chatgpt-ssh-mcp}"
cd "$ROOT"
export SSH_MCP_CONFIG="${SSH_MCP_CONFIG:-$HOME/.config/ssh-mcp/servers.toml}"
export SSH_MCP_TRANSPORT=http
export SSH_MCP_HTTP_HOST=127.0.0.1
export SSH_MCP_HTTP_PORT="${SSH_MCP_HTTP_PORT:-8000}"
export SSH_MCP_HTTP_AUTH=bearer
export SSH_MCP_HTTP_TOKEN_FILE="${SSH_MCP_HTTP_TOKEN_FILE:-$HOME/.config/ssh-mcp/http-token}"
[[ -f "$SSH_MCP_CONFIG" ]] || { echo "Missing config: $SSH_MCP_CONFIG" >&2; exit 1; }
[[ -f "$SSH_MCP_HTTP_TOKEN_FILE" ]] || { echo "Missing HTTP token file (never print its contents)" >&2; exit 1; }
[[ -x "$ENV_PREFIX/bin/blc-ssh-mcp" ]] || { echo "Conda executable missing: $ENV_PREFIX/bin/blc-ssh-mcp; set SSH_MCP_CONDA_PREFIX" >&2; exit 1; }
export PATH="$ENV_PREFIX/bin:$PATH"
exec "$ENV_PREFIX/bin/blc-ssh-mcp"
