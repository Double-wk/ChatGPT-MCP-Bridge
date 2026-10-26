#!/usr/bin/env bash
# Run on the GPU server as the unprivileged user owning the project.
set -Eeuo pipefail
ROOT="${MCP_BRIDGE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_NAME="${SSH_MCP_CONDA_NAME:-chatgpt-mcp-bridge}"
# Use conda-forge by default so setup does not require accepting Anaconda defaults ToS.
CONDA_CHANNEL="${SSH_MCP_CONDA_CHANNEL:-conda-forge}"
CONFIG_DIR="${HOME}/.config/ssh-mcp"
[[ -f "$ROOT/pyproject.toml" ]] || { echo "Missing project: $ROOT" >&2; exit 1; }
command -v conda >/dev/null 2>&1 || { echo 'Initialize conda first' >&2; exit 1; }
if ! conda env list --json | python3 -c 'import json,os,sys; n=sys.argv[1]; d=json.load(sys.stdin)["envs"]; sys.exit(0 if any(os.path.basename(x.rstrip("/"))==n for x in d) else 1)' "$ENV_NAME"; then
  echo "Creating Conda environment $ENV_NAME from $CONDA_CHANNEL (excluding default channels)"
  conda create -y -n "$ENV_NAME" --override-channels -c "$CONDA_CHANNEL" python=3.11
fi
bash "$ROOT/deploy/build-runtime.sh"
conda run --no-capture-output -n "$ENV_NAME" python -m pip install -e "$ROOT[gateway]"
install -d -m 700 "$CONFIG_DIR"
if [[ ! -e "$CONFIG_DIR/http-token" ]]; then
  umask 077
  conda run --no-capture-output -n "$ENV_NAME" python -c 'import secrets; print(secrets.token_urlsafe(48))' > "$CONFIG_DIR/http-token"
  chmod 600 "$CONFIG_DIR/http-token"
  echo 'Created private token file (secret not printed)'
else
  chmod 600 "$CONFIG_DIR/http-token"
  echo 'Preserved existing HTTP token'
fi
echo "Conda environment: $ENV_NAME"
echo 'Set CODEX_MCP_WORKSPACE to the directory you want to operate on.'
echo 'Then start: bash deploy/run-http.sh'
