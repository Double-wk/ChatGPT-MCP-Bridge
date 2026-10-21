#!/usr/bin/env bash
# Run on the GPU server as the unprivileged user owning the project.
set -Eeuo pipefail
ROOT="${SSH_MCP_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_NAME="${SSH_MCP_CONDA_NAME:-chatgpt-ssh-mcp}"
# Use conda-forge by default so setup does not require accepting Anaconda defaults ToS.
CONDA_CHANNEL="${SSH_MCP_CONDA_CHANNEL:-conda-forge}"
CONFIG_DIR="${HOME}/.config/ssh-mcp"
[[ -f "$ROOT/pyproject.toml" ]] || { echo "Missing project: $ROOT" >&2; exit 1; }
command -v conda >/dev/null 2>&1 || { echo 'Initialize conda first' >&2; exit 1; }
if ! conda env list --json | python3 -c 'import json,os,sys; n=sys.argv[1]; d=json.load(sys.stdin)["envs"]; sys.exit(0 if any(os.path.basename(x.rstrip("/"))==n for x in d) else 1)' "$ENV_NAME"; then
  echo "Creating Conda environment $ENV_NAME from $CONDA_CHANNEL (excluding default channels)"
  conda create -y -n "$ENV_NAME" --override-channels -c "$CONDA_CHANNEL" python=3.11
fi
conda run --no-capture-output -n "$ENV_NAME" python -m pip install -e "$ROOT"
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
if [[ ! -e "$CONFIG_DIR/servers.toml" ]]; then
  cat > "$CONFIG_DIR/servers.toml" <<'TOML'
[settings]
ssh_config_path = "~/.ssh/config"
known_hosts = true
command_timeout = 30

[groups]
gpu = { description = "GPU Linux hosts" }

[servers.gpu-local]
description = "GPU host via its local SSH daemon"
groups = ["gpu"]
default_dir = "/home/test/Double"
TOML
  chmod 600 "$CONFIG_DIR/servers.toml"
  echo "Configure SSH Host gpu-local in ~/.ssh/config"
else
  echo 'Preserved existing server inventory'
fi
echo "Conda environment: $ENV_NAME"
echo 'Verify: ssh -o BatchMode=yes gpu-local hostname'
echo 'Then start: bash deploy/run-http.sh'
