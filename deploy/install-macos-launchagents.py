#!/usr/bin/env python3
"""Install and start this user's MCP/Gateway launch agents on macOS."""

import os
import plistlib
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def listening(port):
    with socket.socket() as connection:
        connection.settimeout(0.5)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def bootstrap_service(domain, path):
    # launchd can briefly return EIO while the previous service unloads.
    for attempt in range(10):
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        if result.returncode != 5 or attempt == 9:
            raise RuntimeError(f"Cannot start {path.name}: {result.stderr.strip()}")
        time.sleep(0.2)


def main():
    if sys.platform != "darwin":
        raise SystemExit("This installer is for macOS.")
    root = Path(__file__).resolve().parent.parent
    prefix = Path(sys.prefix)
    if not (prefix / "conda-meta").is_dir():
        raise SystemExit("Run this installer with the project's Conda Python.")
    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    logs = Path.home() / "Library/Logs/chatgpt-mcp-bridge"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs.chmod(0o700)
    workspace = (
        Path(os.environ.get("MCP_BRIDGE_WORKSPACE", str(Path.home() / "Projects")))
        .expanduser()
        .resolve()
    )
    workspace.mkdir(parents=True, exist_ok=True)
    config_dir = Path.home() / ".config/ssh-mcp"
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    gateway_launcher = config_dir / "start-gateway-macos.sh"
    gateway_launcher.write_text(
        "#!/bin/bash\nset -Eeuo pipefail\n"
        'set -a\n. "$HOME/.config/ssh-mcp/github-oauth.env"\nset +a\n'
        'exec "$MCP_BRIDGE_ENV_PREFIX/bin/python" -m uvicorn '
        "chatgpt_mcp_bridge.gateway.app:app --host 127.0.0.1 --port 8200\n"
    )
    gateway_launcher.chmod(0o700)
    domain = f"gui/{os.getuid()}"
    environment = {
        "MCP_BRIDGE_ENV_PREFIX": str(prefix),
        "MCP_BRIDGE_WORKSPACE": str(workspace),
        "MCP_BRIDGE_FULL_ACCESS": os.environ.get("MCP_BRIDGE_FULL_ACCESS", "false"),
        "MCP_BRIDGE_NETWORK_ACCESS": os.environ.get(
            "MCP_BRIDGE_NETWORK_ACCESS", "false"
        ),
        "MCP_BRIDGE_TRANSPORT": "http",
        "MCP_BRIDGE_HTTP_HOST": "127.0.0.1",
        "MCP_BRIDGE_HTTP_PORT": "8000",
        "MCP_BRIDGE_HTTP_AUTH": "bearer",
        "MCP_BRIDGE_HTTP_TOKEN_FILE": str(config_dir / "http-token"),
        "HOME": str(Path.home()),
        "PATH": f"{prefix}/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    for name, script, port, marker in [
        ("mcp", "run-http.sh", 8000, str(prefix / "bin/chatgpt-mcp-bridge")),
        ("gateway", "start-gateway.sh", 8200, "chatgpt_mcp_bridge.gateway.app:app"),
    ]:
        label = f"com.double.chatgpt-mcp-bridge.{name}"
        path = agents / f"{label}.plist"
        log = logs / f"{name}.log"
        log.touch(mode=0o600, exist_ok=True)
        log.chmod(0o600)
        config = {
            "Label": label,
            "ProgramArguments": (
                [str(prefix / "bin/chatgpt-mcp-bridge")]
                if name == "mcp"
                else ["/bin/bash", str(gateway_launcher)]
            ),
            "WorkingDirectory": str(workspace),
            "EnvironmentVariables": environment,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 5,
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        }
        path.write_bytes(plistlib.dumps(config))
        subprocess.run(["plutil", "-lint", str(path)], check=True)
        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{label}"], capture_output=True
        )
        pidfile = root / "logs" / f"{name}.pid"
        if pidfile.exists():
            pid = int(pidfile.read_text().strip())
            command = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
            ).stdout
            if marker in command:
                os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not listening(port):
                break
            time.sleep(0.1)
        if listening(port):
            raise SystemExit(f"Port {port} is occupied; no duplicate service started.")
        bootstrap_service(domain, path)
        for _ in range(100):
            if listening(port):
                break
            time.sleep(0.1)
        if not listening(port):
            raise SystemExit(f"{name} failed to start; inspect {log}.")
        print(f"Started {label} on loopback port {port}")

    app = Path("/Applications/Tailscale.app")
    if app.is_dir():
        label = "com.double.chatgpt-mcp-bridge.tailscale"
        path = agents / f"{label}.plist"
        path.write_bytes(
            plistlib.dumps(
                {
                    "Label": label,
                    "ProgramArguments": ["/usr/bin/open", "-g", str(app)],
                    "RunAtLoad": True,
                }
            )
        )
        subprocess.run(["plutil", "-lint", str(path)], check=True)
        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{label}"], capture_output=True
        )
        bootstrap_service(domain, path)
        print("Tailscale will open in the background at this user's login.")


if __name__ == "__main__":
    main()
