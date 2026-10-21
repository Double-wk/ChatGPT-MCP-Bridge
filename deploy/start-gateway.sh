#!/usr/bin/env bash
# Launch OAuth gateway from the project root and private credentials.
set -Eeuo pipefail
ROOT="${SSH_MCP_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_PREFIX="${SSH_MCP_CONDA_PREFIX:-/mnt/tcm_agent_data/Double/envs/chatgpt-ssh-mcp}"
OAUTH_FILE="${GATEWAY_OAUTH_ENV_FILE:-$HOME/.config/ssh-mcp/github-oauth.env}"
[[ -f "$OAUTH_FILE" ]] || { echo "Missing private OAuth environment file" >&2; exit 1; }
[[ -x "$ENV_PREFIX/bin/python" ]] || { echo "Missing Conda Python" >&2; exit 1; }
cd "$ROOT"
set -a
. "$OAUTH_FILE"
set +a
: "${GITHUB_CLIENT_ID:?Missing GitHub OAuth client ID}"
: "${GITHUB_CLIENT_SECRET:?Missing GitHub OAuth client secret}"
: "${GITHUB_ALLOWED_IDS:?Missing GitHub numeric allowlist}"
: "${GATEWAY_PUBLIC_BASE:?Missing public HTTPS base}"
export CHATGPT_OAUTH_CLIENT_ID="${CHATGPT_OAUTH_CLIENT_ID:-chatgpt-mcp-bridge}"
export PYTHONPATH="$ROOT"
exec "$ENV_PREFIX/bin/python" -m uvicorn gateway.app:app --host 127.0.0.1 --port "${GATEWAY_HTTP_PORT:-8200}"
