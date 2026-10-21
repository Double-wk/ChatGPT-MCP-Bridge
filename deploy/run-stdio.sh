#!/usr/bin/env bash
# Intended for a Secure MCP Tunnel's --mcp-command. Runs on GPU Linux, not Mac.
# stdout MUST contain only MCP protocol responses; diagnostics go to stderr.
set -Eeuo pipefail
ROOT="${SSH_MCP_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_PREFIX="${SSH_MCP_CONDA_PREFIX:-/mnt/tcm_agent_data/Double/envs/chatgpt-ssh-mcp}"
cd "$ROOT"
export SSH_MCP_CONFIG="${SSH_MCP_CONFIG:-$HOME/.config/ssh-mcp/servers.toml}"
export SSH_MCP_TRANSPORT=stdio
[[ -f "$SSH_MCP_CONFIG" ]] || { echo "Missing SSH server inventory" >&2; exit 1; }
[[ -x "$ENV_PREFIX/bin/blc-ssh-mcp" ]] || { echo "Missing Conda MCP executable: $ENV_PREFIX/bin/blc-ssh-mcp" >&2; exit 1; }
export PATH="$ENV_PREFIX/bin:$PATH"
exec "$ENV_PREFIX/bin/blc-ssh-mcp"
