"""Project configuration, including compatibility with previous deployments."""

import os
from pathlib import Path


def getenv(name, default=None):
    aliases = {
        "CODEX_MCP_WORKSPACE": "MCP_BRIDGE_WORKSPACE",
        "CODEX_MCP_HOME": "MCP_BRIDGE_EXECUTOR_HOME",
        "CODEX_MCP_NETWORK_ACCESS": "MCP_BRIDGE_NETWORK_ACCESS",
    }
    canonical = aliases.get(name, name.replace("SSH_MCP_", "MCP_BRIDGE_", 1))
    return os.environ.get(canonical, os.environ.get(name, default))


def state_dir():
    return (
        Path(
            os.environ.get(
                "MCP_BRIDGE_STATE_DIR",
                str(Path.home() / ".local/state/chatgpt-mcp-bridge"),
            )
        )
        .expanduser()
        .resolve()
    )
