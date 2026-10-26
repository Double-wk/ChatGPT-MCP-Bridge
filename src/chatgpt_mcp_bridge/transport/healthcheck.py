"""Liveness healthcheck for ssh-mcp Docker container.

Invoked as ``ssh-mcp healthcheck`` from the Dockerfile HEALTHCHECK
directive. Exits 0 if the server is healthy, 1 otherwise. Prints a
single diagnostic line to stderr on failure (never logs the token).

Auto-detects transport via ``SSH_MCP_TRANSPORT`` env var:
  * ``stdio`` (default): import check + config file parse
  * ``http`` / ``streamable-http``: MCP initialize POST handshake
  * any other value: also falls through to the stdio check. That is
    *laxer* than the server, which raises ``Unknown
    SSH_MCP_TRANSPORT=...`` and refuses to start (see
    ``server.py::main``) — so a container that cannot boot because of a
    typo'd transport (``htpp``) still passes this liveness gate.

HTTP mode reads the same env vars as the server: ``SSH_MCP_HTTP_PORT``
(default ``8000``), ``SSH_MCP_HTTP_AUTH`` (``none`` suppresses the
bearer token), ``SSH_MCP_HTTP_TOKEN`` and ``SSH_MCP_HTTP_TOKEN_FILE``.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

from ..config import getenv

HEALTHCHECK_TIMEOUT = 3  # seconds


def _load_token() -> str | None:
    """Read bearer token from env or token file.

    Returns None if neither is set — and also if the token file is
    unreadable or blank, so unset and broken are indistinguishable to the
    caller. Note the asymmetry with ``server.py::_run_http``, which raises
    RuntimeError on an unreadable ``SSH_MCP_HTTP_TOKEN_FILE``: here a
    missing token only means the handshake goes out unauthenticated, and
    the resulting 401 still counts as alive.
    """
    raw = getenv("SSH_MCP_HTTP_TOKEN", "").strip()
    if raw:
        return raw
    token_file = getenv("SSH_MCP_HTTP_TOKEN_FILE", "").strip()
    if token_file:
        try:
            return Path(token_file).read_text().strip() or None
        except OSError:
            return None
    return None


def _check_stdio() -> tuple[bool, str]:
    """Check local workspace and Codex binary without loading SSH configuration."""
    try:
        from ..runtime.executor import ExecutionRuntime

        ExecutionRuntime()
    except Exception as exc:
        return False, str(exc)
    return True, "Codex binary and workspace available (execution not probed)"


def _check_http() -> tuple[bool, str]:
    """Send MCP initialize POST and verify the server responds.

    Returns (ok, diagnostic). Any non-5xx status is considered healthy
    (including 401 if auth is misconfigured — the server is clearly alive).
    """
    port = getenv("SSH_MCP_HTTP_PORT", "8000")
    auth_mode = getenv("SSH_MCP_HTTP_AUTH", "bearer").strip().lower()
    token = _load_token() if auth_mode != "none" else None

    # Two values below are implicit couplings to SDK defaults, not knobs:
    # "2025-03-26" is the MCP protocol revision this probe claims, and
    # ``/mcp`` is MCPServer's default streamable_http_path (server.py never
    # overrides it and mounts the app at "/"). If either default moves,
    # this probe must be updated in lockstep or it stops reaching the app.
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ssh-mcp-healthcheck", "version": "1"},
            },
        }
    ).encode()

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"http://127.0.0.1:{port}/mcp"
    req = urllib.request.Request(url, data=payload, method="POST", headers=headers)  # noqa: S310

    try:
        # Scheme, host and path are hardcoded http://127.0.0.1/mcp — never
        # a file:// scheme and not user-controlled, which is what makes
        # B310 (permitted-schemes) moot. The port segment *is* env-derived
        # (SSH_MCP_HTTP_PORT) and is not validated here: no int(), no range
        # check — that validation lives in server.py::_run_http and runs in
        # the server process. A junk value only yields an unreachable URL
        # and an unhealthy verdict.
        with urllib.request.urlopen(req, timeout=HEALTHCHECK_TIMEOUT) as resp:  # nosec B310  # noqa: S310
            return True, f"http {resp.status}"
    except urllib.error.HTTPError as e:
        # Any non-5xx status means the server is alive but the request was rejected
        # (wrong auth, wrong protocol version, etc.) — still healthy.
        return e.code < 500, f"http {e.code}"
    except urllib.error.URLError as e:
        return False, f"connect failed: {type(e.reason).__name__}"
    except Exception as e:  # noqa: BLE001 - an unknown probe failure is 'unhealthy', not a crash of the healthcheck itself
        return False, f"unexpected: {type(e).__name__}"


def run() -> NoReturn:
    """Entry point invoked by ``ssh-mcp healthcheck`` CLI."""
    transport = getenv("SSH_MCP_TRANSPORT", "stdio").strip().lower()
    if transport in ("http", "streamable-http"):
        ok, diag = _check_http()
    else:
        ok, diag = _check_stdio()

    if not ok:
        print(f"ssh-mcp healthcheck: UNHEALTHY ({diag})", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    run()
